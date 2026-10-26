#!/usr/bin/env python3
"""Collect OpenWrt/TACO versions over SSH, write JSON/raw evidence and a report."""
import argparse
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

OPENWRT_VERSIONS = "https://downloads.openwrt.org/.versions.json"
PRELUDE = r'''
export LC_ALL=C
section() {
    label="$1"
    shift
    printf '\n@@BEGIN:%s\n' "$label"
    "$@" 2>&1
    result=$?
    printf '\n@@END:%s:%s\n' "$label" "$result"
}
section hostname cat /proc/sys/kernel/hostname
section kernel uname -r
section architecture uname -m
section os_release cat /etc/os-release
'''
OPENWRT_SCRIPT = PRELUDE + r'''
section board ubus call system board
section openwrt_release cat /etc/openwrt_release
if command -v opkg >/dev/null 2>&1; then
    section package_manager opkg --version
    section packages opkg list-installed
elif command -v apk >/dev/null 2>&1; then
    section package_manager apk --version
    section packages apk list --installed
else
    section packages sh -c 'echo "No supported package manager found"; exit 1'
fi
'''
TACO_SCRIPT = PRELUDE + r'''
section packages dpkg-query -W '-f=${binary:Package}\t${Version}\n'
section apt_index_files find /var/lib/apt/lists -maxdepth 1 -type f -name '*InRelease' -printf '%T@ %f\n'
section apt_candidates apt-get -s -o Debug::NoLocking=1 upgrade
'''


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_sections(raw):
    sections = {}
    pattern = r"(?:^|\n)@@BEGIN:([a-z_]+)\n(.*?)\n@@END:\1:(\d+)(?:\n|$)"
    for match in re.finditer(pattern, raw, re.DOTALL):
        sections[match[1]] = {"returncode": int(match[3]), "output": match[2].strip()}
    return sections


def value(sections, name):
    section = sections.get(name, {})
    return section.get("output", "") if section.get("returncode") == 0 else ""


def os_name(sections):
    text = value(sections, "os_release")
    match = re.search(r'^PRETTY_NAME=["\x27]?(.*?)["\x27]?$', text, re.MULTILINE)
    return match[1] if match else "不明（os_releaseを参照）"


def release_version(text):
    found = re.search(r"^DISTRIB_RELEASE=['\"]?([^'\"\n]+)", text, re.MULTILINE)
    return found[1] if found else None


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return None
    return tuple(map(int, version.split(".")))


def compare_openwrt(installed, metadata):
    stable = metadata.get("stable_version")
    current, newest = version_tuple(installed), version_tuple(stable)
    result = {"installed": installed, "latest_stable": stable, "status": "unknown",
              "same_series_latest": None,
              "note": "公式安定版との比較。機種対応・移行手順は別途確認。自動更新しません。"}
    if current:
        versions = [v for v in metadata.get("versions_list", [])
                    if version_tuple(v) and version_tuple(v)[:2] == current[:2]]
        if versions:
            result["same_series_latest"] = max(versions, key=version_tuple)
    if current and newest:
        if current < newest:
            result["status"] = "update_available"
            result["upgrade_type"] = "major_series" if current[:2] != newest[:2] else "maintenance"
        elif current == newest:
            result["status"] = "matches_latest_stable"
        else:
            result["status"] = "newer_or_custom"
    return result


def apt_updates(text):
    updates = []
    for line in text.splitlines():
        match = re.match(r"Inst\s+(\S+)\s+(?:\[([^\]]+)\]\s+)?\((\S+)", line)
        if match:
            updates.append({"package": match[1], "installed": match[2], "candidate": match[3]})
    return updates


def fetch_latest():
    request = urllib.request.Request(OPENWRT_VERSIONS, headers={"User-Agent": "development-network-version-check/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("release metadata too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or not version_tuple(data.get("stable_version")):
        raise ValueError("official metadata has no valid stable_version")
    return data


def collect(host, script, timeout):
    started = now()
    try:
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
                               "-o", "ServerAliveCountMax=2", host, "sh", "-s"],
                              input=script, text=True, capture_output=True, timeout=timeout)
        return {"host": host, "collected_at": started, "ssh_returncode": proc.returncode,
                "ssh_error": proc.stderr.strip(), "sections": parse_sections(proc.stdout)}, proc.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"host": host, "collected_at": started, "ssh_returncode": -1,
                "ssh_error": str(exc), "sections": {}}, ""


def analyse_taco(device, refreshed):
    sections = device["sections"]
    candidates = sections.get("apt_candidates", {})
    succeeded = candidates.get("returncode") == 0
    updates = apt_updates(candidates.get("output", "")) if succeeded else []
    refresh_ok = sections.get("apt_refresh", {}).get("returncode") == 0 if refreshed else False
    # apt-get update can return success despite some failed repositories unless
    # Error-Mode=any is set; the refresh script below requests strict behavior.
    status = "unknown"
    if succeeded:
        status = "updates_available" if updates else "no_candidates_in_cache"
    return {"status": status, "updates": updates,
            "metadata": "refreshed" if refresh_ok else "cached_not_verified_latest",
            "refresh_requested": refreshed, "refresh_succeeded": refresh_ok,
            "note": "APT設定済みリポジトリ内のupgrade候補。保留・依存変更が必要な更新は生ログも確認。"
                    "OSの世代更新や上流最新版、ファームウェアの最新性は判定しません。"}


def device_summary(report, name):
    """Green means verified current within the explicitly stated comparison scope."""
    device = report["devices"][name]
    sections = device.get("sections", {})
    analysis = device["assessment"]
    if device.get("ssh_returncode") != 0 or not sections:
        return False, "要確認（SSH／情報取得に失敗）"
    if name == "openwrt":
        if report["latest_openwrt"]["status"] != "ok":
            return False, "判定不能（公式最新版を未取得）"
        return {
            "matches_latest_stable": (True, "最新（公式安定版と一致）"),
            "update_available": (False, "更新あり（新しい公式安定版）"),
            "newer_or_custom": (False, "要確認（公式安定版より新しい／独自版）"),
        }.get(analysis["status"], (False, "判定不能"))
    if analysis["status"] == "unknown" or sections.get("apt_candidates", {}).get("returncode") != 0:
        return False, "判定不能（APT候補を取得できません）"
    if analysis.get("updates"):
        return False, f"更新候補あり（APT {len(analysis['updates'])}件）"
    held = re.search(r"(\d+)\s+not upgraded", value(sections, "apt_candidates"))
    if held and int(held[1]) > 0:
        return False, f"要確認（APTで保留・未更新 {held[1]}件）"
    if analysis.get("metadata") != "refreshed" or not analysis.get("refresh_succeeded"):
        return False, "最新性未確認（APTキャッシュ参照）"
    if analysis["status"] == "no_candidates_in_cache":
        return True, "最新（APT確認範囲・更新候補なし）"
    return False, "要確認（APT更新候補を確認）"


def colored_text(text, healthy, terminal=False, color=True):
    if terminal:
        return f"\033[{32 if healthy else 31}m{text}\033[0m" if color else text
    return f'<span style="color: {"#15803d" if healthy else "#dc2626"};">{html.escape(text)}</span>'


def status_text(text, healthy, terminal=False, color=True):
    return colored_text(("🟢 " if healthy else "🔴 ") + text, healthy, terminal, color)


def terminal_color(mode):
    if mode != "auto":
        return mode == "always"
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def terminal_summary(report, color=True):
    lines = []
    for name in ("openwrt", "taco"):
        device = report["devices"][name]
        analysis = device["assessment"]
        healthy, label = device_summary(report, name)
        label = label.split("（", 1)[0]
        label = {"最新性未確認": "未確認", "判定不能": "取得失敗",
                 "更新候補あり": "更新あり"}.get(label, label)
        if device.get("ssh_returncode") != 0:
            label = "取得失敗"
        if name == "openwrt":
            details = analysis.get("installed") or "—"
            latest = analysis.get("latest_stable")
            if latest and latest != analysis.get("installed"):
                details += " → " + latest
        else:
            if analysis.get("updates"):
                label += f" {len(analysis['updates'])}件"
            details = os_name(device["sections"])
            if details.startswith("不明"):
                details = "—"
            kernel = value(device["sections"], "kernel")
            if kernel:
                details += " / " + kernel
        lines.append(f"{name.upper():<8} " + status_text(label, healthy, terminal=True, color=color) + "  " + details)
    return "\n".join(lines)


def markdown(report, terminal=False, color=True):
    lines = ["# 開発NW バージョン確認", "", f"収集日時（UTC）：{report['collected_at']}", "",
             "この処理はOS・パッケージを更新しません。取得失敗やキャッシュ依存は、更新不要を意味しません。", ""]
    lines += ["## 簡易サマリー", ""]
    for name in ("openwrt", "taco"):
        healthy, label = device_summary(report, name)
        lines.append(f"- {name.upper()}：" + status_text(label, healthy, terminal, color))
    lines += ["", "緑＝確認範囲で最新。赤＝更新あり・要確認・判定不能。TACOはOS世代・ファームウェアの最新性を含みません。", ""]
    latest = report["latest_openwrt"]
    lines += [f"OpenWrt公式情報：{latest['source']}",
              f"取得状態：{latest['status']}" + (" / " + latest["error"] if latest.get("error") else ""), ""]
    for name in ("openwrt", "taco"):
        device = report["devices"][name]
        lines += [f"## {name.upper()}", "", f"接続先：`{device['host']}`",
                  f"SSH結果：{device['ssh_returncode']}", ""]
        if device["ssh_error"]:
            lines += ["SSHメッセージ：", "```text", device["ssh_error"], "```", ""]
        sections = device["sections"]
        lines += [f"OS：{os_name(sections)}", "", f"カーネル：`{value(sections, 'kernel') or '取得失敗'}`",
                  f"アーキテクチャ：`{value(sections, 'architecture') or '取得失敗'}`", ""]
        analysis = device["assessment"]
        healthy, label = device_summary(report, name)
        if name == "openwrt":
            lines += ["判定：" + status_text(label, healthy, terminal, color), "",
                      f"現在：`{analysis.get('installed') or '不明'}` / 最新安定版：`{analysis.get('latest_stable') or '未取得'}`",
                      f"同じ系列の最新：`{analysis.get('same_series_latest') or '不明'}`", "", analysis["note"], "",
                      "OpenWrtの更新は機種・targetに合うsysupgrade等の手順で計画してください。",
                      "この結果を根拠にopkg/apkの全パッケージを一括更新することは勧めません。", ""]
        else:
            lines += ["判定：" + status_text(label, healthy, terminal, color), "",
                      "リポジトリ情報：" + ("今回更新済み" if analysis["metadata"] == "refreshed" else "キャッシュ参照・最新性未確認"),
                      "", analysis["note"], ""]
            if analysis["refresh_requested"] and not analysis["refresh_succeeded"]:
                lines += ["**パッケージ情報の更新に失敗しました。保存済みキャッシュでの候補です。**", ""]
            if analysis["updates"]:
                lines += ["| パッケージ | 現在 | 候補 |", "|---|---|---|"]
                confirmed = analysis["metadata"] == "refreshed" and analysis["refresh_succeeded"]
                for update in analysis["updates"]:
                    installed = colored_text(update["installed"] or "未導入",
                                             confirmed and update["installed"] == update["candidate"], terminal, color)
                    candidate = colored_text(update["candidate"], confirmed, terminal, color)
                    lines.append(f"| {update['package']} | {installed} | {candidate} |")
                lines += ["", "現在の旧バージョンは赤。候補は今回のAPT情報更新が成功した場合に緑、キャッシュ参照など最新性未確認なら赤。", ""]
        missing = [key for key, section in sections.items() if section["returncode"]]
        if missing:
            lines += [status_text("収集上の注意：" + ", ".join(missing) + " の取得に失敗。JSON／生ログを確認。",
                                  False, terminal, color), ""]
    lines += ["## Codexに確認を依頼する場合", "",
              "このフォルダのversions.json、report.md、openwrt.raw.txt、taco.raw.txtを参照し、",
              "機種・OS系列に対応する最新公式情報と照合して、更新要否と移行上の注意点を確認してください。", "",
              "SSH先・ホスト名・パッケージ一覧を含むため、共有範囲に注意してください。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openwrt", required=True, help="SSH alias or user@host")
    parser.add_argument("--taco", required=True, help="SSH alias or user@host")
    parser.add_argument("--output", type=Path, default=Path("version-reports"))
    parser.add_argument("--offline", action="store_true", help="skip official online release lookup; SSH collection still runs")
    parser.add_argument("--refresh-apt", action="store_true", help="explicitly run sudo -n apt-get update on TACO (metadata only)")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                        help="stdout ANSI color (auto: terminal only); report.md always uses HTML color + status symbols")
    args = parser.parse_args()
    for host in (args.openwrt, args.taco):
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", host):
            parser.error("SSH先は英数字・._@-のSSH別名またはuser@hostを指定してください。ポート等は~/.ssh/configで設定。")
    if args.timeout < 10:
        parser.error("timeout must be at least 10 seconds")
    os.umask(0o077)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = args.output / stamp
    directory.mkdir(parents=True, mode=0o700)
    report = {"schema_version": 1, "collected_at": now(), "devices": {},
              "latest_openwrt": {"source": OPENWRT_VERSIONS, "status": "skipped", "data": {}}}
    for name, host, script in [("openwrt", args.openwrt, OPENWRT_SCRIPT), ("taco", args.taco, TACO_SCRIPT)]:
        if name == "taco" and args.refresh_apt:
            script = script.replace("section apt_candidates", "section apt_refresh sudo -n apt-get -o APT::Update::Error-Mode=any update\nsection apt_candidates")
        device, raw = collect(host, script, args.timeout)
        report["devices"][name] = device
        (directory / f"{name}.raw.txt").write_text(raw)
    if not args.offline:
        try:
            metadata = fetch_latest()
            report["latest_openwrt"].update(status="ok", data=metadata, fetched_at=now())
        except (OSError, ValueError) as exc:
            report["latest_openwrt"].update(status="failed", error=str(exc))
    ow = report["devices"]["openwrt"]
    installed = release_version(value(ow["sections"], "openwrt_release"))
    if not installed:
        try:
            installed = json.loads(value(ow["sections"], "board")).get("release", {}).get("version")
        except (ValueError, AttributeError):
            pass
    ow["assessment"] = compare_openwrt(installed, report["latest_openwrt"]["data"])
    report["devices"]["taco"]["assessment"] = analyse_taco(report["devices"]["taco"], args.refresh_apt)
    (directory / "versions.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    summary = markdown(report)
    (directory / "report.md").write_text(summary)
    print(terminal_summary(report, color=terminal_color(args.color)))
    print()
    print(f"保存先：{directory.resolve()}")
    # Partial SSH/command failures are visible to automation, even if another host succeeded.
    failed = any(device["ssh_returncode"] or not device["sections"] or
                 any(s["returncode"] for s in device["sections"].values())
                 for device in report["devices"].values())
    failed = failed or report["latest_openwrt"]["status"] == "failed"
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
