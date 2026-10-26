import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import contextlib
import sys
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "taco-console"))


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, str(file))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


console = module("console", BASE / "taco-console/console.py")
versions = module("versions", BASE / "check_versions.py")
control = module("gate", BASE / "taco-console/control.py")


class Connections(unittest.TestCase):
    def test_taco_downlink_is_not_counted_as_an_ai_endpoint(self):
        self.assertEqual(console.scope("10.1.0.1"), "TACO")
        self.assertEqual(console.scope("10.2.0.2"), "TACO")
        self.assertEqual(console.scope("10.1.0.10"), "AI")

    def test_original_reply_and_accounting(self):
        line = "ipv4 2 tcp 6 431999 ESTABLISHED src=10.1.0.10 dst=1.1.1.1 sport=51234 dport=443 packets=2 bytes=150 src=1.1.1.1 dst=10.1.0.10 sport=443 dport=51234 packets=4 bytes=700 [ASSURED] mark=0 id=42"
        flow, = console.parse_conntrack(line)
        self.assertEqual((flow["src"], flow["dst"], flow["sport"], flow["dport"]), ("10.1.0.10", "1.1.1.1", 51234, 443))
        self.assertEqual((flow["tx_bytes"], flow["rx_bytes"]), (150, 700))
        self.assertEqual(flow["state"], "ESTABLISHED")

    def test_filter_and_missing_counters(self):
        lines = "\n".join([
            "udp 17 29 src=10.1.0.10 dst=10.0.0.254 sport=42 dport=53 [UNREPLIED] src=10.0.0.254 dst=10.1.0.10 sport=53 dport=42 mark=0 id=3",
            "tcp 6 10 ESTABLISHED src=10.0.0.9 dst=10.2.0.2 sport=50 dport=22 src=10.2.0.2 dst=10.0.0.9 sport=22 dport=50 id=4",
            "tcp 6 10 ESTABLISHED src=10.2.0.2 dst=1.1.1.1 sport=52 dport=443 src=1.1.1.1 dst=10.2.0.2 sport=443 dport=52 id=5"])
        flows = console.parse_conntrack(lines)
        self.assertEqual(len(flows), 2)
        self.assertEqual(flows[0]["state"], "UNREPLIED")
        self.assertIsNone(flows[0]["tx_bytes"])
        self.assertEqual(flows[1]["scope"], "TACO")

    def test_icmp_identity(self):
        flow, = console.parse_conntrack("icmp 1 29 src=10.1.0.10 dst=10.1.0.1 type=8 code=0 id=20 src=10.1.0.1 dst=10.1.0.10 type=0 code=0 id=20 mark=0 id=100")
        self.assertEqual(flow["proto"], "ICMP")
        self.assertIsNone(flow["dport"])

    def test_journal_only_ai_scoped_drop_messages(self):
        entry = {"__REALTIME_TIMESTAMP": "1792962000000000", "MESSAGE": "taco-forward-drop IN=eth1 OUT=eth0 SRC=10.1.0.10 DST=10.0.0.9 PROTO=TCP SPT=123 DPT=22"}
        unrelated = dict(entry, MESSAGE="unrelated SRC=10.1.0.10")
        rows = console.parse_journal(json.dumps(entry) + "\n" + json.dumps(unrelated) + "\nnot json")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dport"], "22")

    def test_history_closes_missing_and_retains_first_observation(self):
        db = sqlite3.connect(":memory:")
        flow = console.demo_state()["flows"][0]
        console.update_history(db, [flow], "2026-10-26T00:00:00+00:00")
        history = console.update_history(db, [flow], "2026-10-26T00:00:05+00:00")
        self.assertEqual(history[0]["first_seen"], "2026-10-26T00:00:00+00:00")
        history = console.update_history(db, [], "2026-10-26T00:00:10+00:00")
        self.assertEqual(history[0]["ended_at"], "2026-10-26T00:00:10+00:00")
        console.update_history(db, [], "2026-10-28T00:00:00+00:00")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM flows").fetchone()[0], 0)

    def test_collection_error_does_not_claim_empty_healthy_network(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(":memory:")
            with patch.object(console, "command", side_effect=[("", "permission denied"), ("", None)]):
                file = Path(directory) / "state.json"
                console.collect_once(db, file, 5)
                data = json.loads(file.read_text())
                self.assertFalse(data["collector_ok"])
                self.assertIn("permission denied", data["errors"])

    def test_empty_journal_match_is_not_collector_error(self):
        proc = subprocess.CompletedProcess([], 1, "", "")
        with patch.object(console.subprocess, "run", return_value=proc):
            self.assertEqual(console.command(["journalctl"], empty_ok=True), ("", None))
            self.assertIsNotNone(console.command(["conntrack"])[1])


class Versions(unittest.TestCase):
    def color_report(self):
        return {"latest_openwrt": {"status": "ok"}, "devices": {
            "openwrt": {"ssh_returncode": 0, "sections": {"openwrt_release": {"returncode": 0}},
                        "assessment": {"status": "matches_latest_stable"}},
            "taco": {"ssh_returncode": 0, "sections": {"apt_candidates": {"returncode": 0, "output": "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded."}},
                     "assessment": {"status": "no_candidates_in_cache", "updates": [], "metadata": "refreshed", "refresh_succeeded": True}}
        }}

    def test_latest_is_green_but_cache_and_held_updates_are_red(self):
        report = self.color_report()
        self.assertTrue(versions.device_summary(report, "openwrt")[0])
        self.assertTrue(versions.device_summary(report, "taco")[0])
        report["devices"]["taco"]["assessment"]["metadata"] = "cached_not_verified_latest"
        self.assertFalse(versions.device_summary(report, "taco")[0])
        report["devices"]["taco"]["assessment"]["metadata"] = "refreshed"
        report["devices"]["taco"]["sections"]["apt_candidates"]["output"] = "0 upgraded, 2 not upgraded."
        self.assertFalse(versions.device_summary(report, "taco")[0])

    def test_updates_unknown_and_collection_errors_are_red(self):
        report = self.color_report()
        for status in ("update_available", "unknown", "newer_or_custom"):
            report["devices"]["openwrt"]["assessment"]["status"] = status
            self.assertFalse(versions.device_summary(report, "openwrt")[0])
        report["devices"]["openwrt"]["assessment"]["status"] = "matches_latest_stable"
        report["latest_openwrt"]["status"] = "failed"
        self.assertFalse(versions.device_summary(report, "openwrt")[0])
        report["latest_openwrt"]["status"] = "ok"
        report["devices"]["openwrt"]["ssh_returncode"] = 255
        self.assertFalse(versions.device_summary(report, "openwrt")[0])

    def test_markdown_and_terminal_use_separate_color_formats(self):
        self.assertIn("color: #15803d", versions.status_text("最新", True))
        self.assertIn("color: #dc2626", versions.status_text("要確認", False))
        self.assertEqual(versions.status_text("最新", True, terminal=True), "\033[32m🟢 最新\033[0m")
        self.assertEqual(versions.status_text("要確認", False, terminal=True), "\033[31m🔴 要確認\033[0m")
        self.assertNotIn("\033", versions.status_text("最新", True))
        self.assertNotIn("<span", versions.status_text("最新", True, terminal=True))
        self.assertEqual(versions.status_text("最新", True, terminal=True, color=False), "🟢 最新")

    def test_auxiliary_collection_failure_does_not_change_version_result(self):
        report = self.color_report()
        report["devices"]["openwrt"]["sections"]["hostname"] = {"returncode": 127, "output": "sh: hostname: not found"}
        report["devices"]["taco"]["sections"]["packages"] = {"returncode": 1, "output": "collection failed"}
        self.assertTrue(versions.device_summary(report, "openwrt")[0])
        self.assertTrue(versions.device_summary(report, "taco")[0])
        report["devices"]["taco"]["sections"]["apt_candidates"]["returncode"] = 100
        self.assertFalse(versions.device_summary(report, "taco")[0])

    def test_package_version_colors_and_separate_collection_warning(self):
        report = self.color_report()
        report.update(collected_at="2026-10-26T00:00:00+00:00")
        report["latest_openwrt"]["source"] = versions.OPENWRT_VERSIONS
        for device in report["devices"].values():
            device.update(host="test-host", ssh_error="")
            device["assessment"]["note"] = "test note"
        report["devices"]["openwrt"]["sections"]["hostname"] = {"returncode": 127, "output": "hostname missing"}
        analysis = report["devices"]["taco"]["assessment"]
        analysis.update(refresh_requested=True, updates=[{"package": "test-package", "installed": "1.0", "candidate": "1.1"}])
        rendered = versions.markdown(report)
        self.assertIn('color: #dc2626;">1.0</span>', rendered)
        self.assertIn('color: #15803d;">1.1</span>', rendered)
        self.assertIn("収集上の注意：hostname", rendered)
        self.assertIn("🟢 最新（公式安定版と一致）", rendered)
        terminal = versions.markdown(report, terminal=True)
        self.assertIn("\033[31m1.0\033[0m", terminal)
        self.assertIn("\033[32m1.1\033[0m", terminal)
        self.assertNotIn("<span", terminal)
        analysis.update(metadata="cached_not_verified_latest", refresh_succeeded=False)
        self.assertIn('color: #dc2626;">1.1</span>', versions.markdown(report))

    def test_shell_section_framing(self):
        script = versions.PRELUDE + "\nsection test printf '%s' 'value'\nsection failure sh -c 'exit 7'\n"
        proc = subprocess.run(["sh"], input=script, text=True, capture_output=True)
        parsed = versions.parse_sections(proc.stdout)
        self.assertEqual(parsed["test"], {"returncode": 0, "output": "value"})
        self.assertEqual(parsed["failure"]["returncode"], 7)

    def test_stable_comparison_and_same_branch(self):
        metadata = {"stable_version": "25.12.3", "versions_list": ["24.10.9", "24.10.10", "24.10.11-rc1", "25.12.3"]}
        result = versions.compare_openwrt("24.10.8", metadata)
        self.assertEqual(result["same_series_latest"], "24.10.10")
        self.assertEqual(result["upgrade_type"], "major_series")
        self.assertEqual(versions.compare_openwrt("SNAPSHOT", metadata)["status"], "unknown")
        self.assertEqual(versions.compare_openwrt("25.12.3", metadata)["status"], "matches_latest_stable")

    def test_offline_unknown_and_cached_not_current(self):
        self.assertEqual(versions.compare_openwrt("24.10.8", {})["status"], "unknown")
        assessment = versions.analyse_taco({"sections": {"apt_candidates": {"returncode": 0, "output": "0 upgraded"}}}, False)
        self.assertEqual(assessment["status"], "no_candidates_in_cache")
        self.assertEqual(assessment["metadata"], "cached_not_verified_latest")

    def test_failed_apt_refresh_preserves_warning(self):
        device = {"sections": {"apt_candidates": {"returncode": 0, "output": "Inst openssh-server [1.0] (1.1 Debian:stable [arm64])"},
                               "apt_refresh": {"returncode": 100, "output": "fetch failed"}}}
        result = versions.analyse_taco(device, True)
        self.assertFalse(result["refresh_succeeded"])
        self.assertEqual(result["updates"][0]["candidate"], "1.1")

    def test_dpkg_format_uses_actual_escapes(self):
        self.assertIn(r"'-f=${binary:Package}\t${Version}\n'", versions.TACO_SCRIPT)

    def test_complete_cli_writes_report_and_partial_failure(self):
        def fixture(host, script, timeout):
            if host == "ow":
                return {"host": host, "ssh_returncode": 0, "ssh_error": "", "sections": {
                    "openwrt_release": {"returncode": 0, "output": "DISTRIB_RELEASE='24.10.8'"},
                    "kernel": {"returncode": 0, "output": "6.6.1"}}}, "openwrt fixture"
            return {"host": host, "ssh_returncode": 255, "ssh_error": "Permission denied", "sections": {}}, ""
        with tempfile.TemporaryDirectory() as directory:
            argv = ["check_versions.py", "--openwrt", "ow", "--taco", "taco", "--output", directory, "--offline"]
            output = io.StringIO()
            with patch.object(sys, "argv", argv), patch.object(versions, "collect", side_effect=fixture), contextlib.redirect_stdout(output):
                code = versions.main()
            self.assertEqual(code, 2)
            folder, = Path(directory).iterdir()
            data = json.loads((folder / "versions.json").read_text())
            self.assertEqual(data["devices"]["openwrt"]["assessment"]["installed"], "24.10.8")
            self.assertEqual(data["devices"]["taco"]["assessment"]["status"], "unknown")
            self.assertIn("Permission denied", (folder / "report.md").read_text())
            self.assertEqual((folder / "versions.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(output.getvalue().splitlines()), 4)
            self.assertIn("OPENWRT", output.getvalue())
            self.assertIn("🔴 取得失敗", output.getvalue())
            self.assertIn("保存先：", output.getvalue())
            self.assertNotIn("この処理は", output.getvalue())
            self.assertNotIn("Permission denied", output.getvalue())
            self.assertNotIn("<span", output.getvalue())


class HTTP(unittest.TestCase):
    def request(self, path, host="127.0.0.1:8765", headers="", state_path=None, demo=True,
                method="GET", body="", handler=None):
        class Connection:
            def __init__(self):
                self.output = bytearray()
            def makefile(self, mode, *args):
                return io.BytesIO(f"{method} {path} HTTP/1.0\r\nHost: {host}\r\n{headers}\r\n{body}".encode())
            def sendall(self, data):
                self.output.extend(data)
            def settimeout(self, _):
                pass
        conn = Connection()
        handler = handler or console.make_handler(state_path or Path("/no-state-file"), 8765, demo)
        handler(conn, ("127.0.0.1", 50000), object())
        return bytes(conn.output)

    def test_demo_api_and_headers(self):
        response = self.request("/api/state")
        head, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b"200 OK", head)
        self.assertIn(b"Content-Security-Policy:", head)
        self.assertTrue(json.loads(body)["demo"])

    def test_reject_rebinding_cross_site_and_path_traversal(self):
        self.assertIn(b"403", self.request("/api/state", host="attacker.example"))
        self.assertIn(b"403", self.request("/api/state", headers="Sec-Fetch-Site: cross-site\r\n"))
        self.assertIn(b"404", self.request("/../console.py"))

    def test_missing_collector_state_is_unavailable(self):
        self.assertIn(b"503", self.request("/api/state", demo=False))

    def test_stale_snapshot_marked(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "state.json"
            data = console.demo_state()
            data["updated_at"] = "2000-01-01T00:00:00+00:00"
            console.atomic_json(file, data)
            response = self.request("/api/state", state_path=file, demo=False)
            self.assertTrue(json.loads(response.split(b"\r\n\r\n", 1)[1])["stale"])

    def test_mutation_requires_same_origin_and_valid_token(self):
        body = '{"enabled":true}'
        headers = f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("x" * 43)
            callback = unittest.mock.Mock(return_value={"enabled": True, "available": True})
            handler = console.make_handler(Path(directory) / "state", 8765, control_callback=callback, token_file=token)
            response = self.request("/api/internet", method="POST", headers=headers, body=body, handler=handler)
            self.assertIn(b"403", response)
            response = self.request("/api/internet", method="POST", headers=headers + "Origin: http://127.0.0.1:8765\r\n", body=body, handler=handler)
            self.assertIn(b"403", response)
            callback.assert_not_called()
            headers += "Origin: http://127.0.0.1:8765\r\nAuthorization: Bearer " + "x" * 43 + "\r\n"
            response = self.request("/api/internet", method="POST", headers=headers, body=body, handler=handler)
            self.assertIn(b"200 OK", response)
            callback.assert_called_once_with("enable")

    def test_mutation_rejects_unexpected_fields_and_non_boolean(self):
        callback = unittest.mock.Mock()
        handler = console.make_handler(Path("/none"), 8765, demo=True, control_callback=callback)
        for body in ['{"enabled":"false"}', '{"enabled":true,"command":"flush ruleset"}', '[]']:
            headers = f"Origin: http://127.0.0.1:8765\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            self.assertIn(b"400", self.request("/api/internet", method="POST", headers=headers, body=body, handler=handler))
        callback.assert_not_called()

    def test_demo_toggle_has_no_real_controller_side_effect(self):
        callback = unittest.mock.Mock()
        handler = console.make_handler(Path("/none"), 8765, demo=True, control_callback=callback)
        body = '{"enabled":true}'
        headers = f"Origin: http://127.0.0.1:8765\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        self.assertIn(b"200 OK", self.request("/api/internet", method="POST", headers=headers, body=body, handler=handler))
        response = self.request("/api/control", handler=handler)
        self.assertTrue(json.loads(response.split(b"\r\n\r\n", 1)[1])["control"]["enabled"])
        callback.assert_not_called()


class Gate(unittest.TestCase):
    def controller(self):
        with patch.object(control, "install_runtime", return_value=42):
            return control.Controller()

    def test_off_is_empty_set_on_has_bounded_lease(self):
        with patch.object(control, "nft") as nft:
            control.apply_enabled(False)
            self.assertNotIn("add element", nft.call_args[0][1])
            control.apply_enabled(True)
            self.assertIn("10.1.0.0/24 timeout 20s", nft.call_args[0][1])
            self.assertNotIn("flush ruleset", nft.call_args[0][1])

    def test_expiry_and_reload_do_not_restore_on(self):
        controller = self.controller()
        controller.desired = True
        with patch.object(control, "read_gate", return_value={"handle": 42, "enabled": False}), patch.object(control, "apply_enabled") as apply:
            controller.tick()
            self.assertFalse(controller.desired)
            apply.assert_not_called()
        controller.desired = True
        with patch.object(control, "read_gate", return_value={"handle": 50, "enabled": False}), patch.object(control, "apply_enabled") as apply:
            controller.status()
            self.assertFalse(controller.desired)
            apply.assert_called_once_with(False)

    def test_nft_failure_stops_renewing_lease(self):
        controller = self.controller()
        controller.desired = True
        with patch.object(control, "read_gate", side_effect=RuntimeError("netlink failed")), patch.object(control, "apply_enabled") as apply:
            controller.tick()
            self.assertFalse(controller.desired)
            self.assertEqual(controller.error, "netlink failed")
            apply.assert_called_once_with(False)

    def test_probe_uses_fixed_addresses_and_preserves_last_success(self):
        success = subprocess.CompletedProcess([], 0, "64 bytes time=0.43 ms", "")
        failed = subprocess.CompletedProcess([], 1, "", "")
        with patch.object(control.subprocess, "run", side_effect=[success, failed]) as run:
            nodes = control.probe_nodes([{"id": "spark", "last_success": "previous"}])
            self.assertEqual(nodes[1]["state"], "up")
            self.assertEqual(nodes[2]["state"], "no_reply")
            self.assertEqual(nodes[2]["last_success"], "previous")
            self.assertEqual(run.call_args_list[1][0][0][-1], "10.1.0.10")

    def test_persistent_install_validates_before_replacing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "nftables.conf"
            original = "flush ruleset\ntable inet taco_filter {}\n"
            config.write_text(original)
            snippet = Path(directory) / "snippet.nft"
            with patch.object(control, "nft", side_effect=RuntimeError("syntax error")):
                with self.assertRaises(RuntimeError):
                    control.install_persistent(config, snippet, Path(directory) / "token")
            self.assertEqual(config.read_text(), original)
            self.assertFalse(snippet.exists())


if __name__ == "__main__":
    unittest.main()
