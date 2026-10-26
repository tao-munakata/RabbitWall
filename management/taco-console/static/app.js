"use strict";
let snapshot = null, tab = "flows";
let control = null, controlBusy = false, controlDemo = false, controlEpoch = 0;
let activeToken = "";
const $ = id => document.getElementById(id);
const heads = {
  flows: ["方向", "送信元", "宛先", "宛先国", "サービス", "状態", "送信 / 受信", "初回観測"],
  history: ["方向", "送信元", "宛先", "宛先国", "サービス", "観測状態", "送信 / 受信", "最終観測"],
  drops: ["方向", "送信元", "宛先", "宛先国", "プロトコル", "チェーン", "受信 → 送信IF", "時刻"]
};
let countryNames;
try { countryNames = new Intl.DisplayNames(["ja"], {type:"region"}); } catch (_) { countryNames = null; }
function countryText(info) {
  if (info?.kind === "private") return "LAN";
  if (info?.kind === "special") return "予約・特殊";
  if (!/^[A-Z]{2}$/.test(info?.code || "")) return "不明";
  const flag = [...info.code].map(c => String.fromCodePoint(127397 + c.charCodeAt(0))).join("");
  return flag + " " + (countryNames?.of(info.code) || info.code) + " · " + info.code;
}
function direction(flow) {
  const taco = ["10.2.0.2", "10.1.0.1"].includes(flow.dst);
  return flow.scope === "MANAGEMENT" ? taco ? "LAN → TACO" : "LAN → AI" : flow.scope === "TACO" ? "TACO発信" : taco ? "AI → TACO" : "AI → 上流";
}
function navigate() {
  const pages = {monitor:["セッション・死活監視", "接続中の通信と、主要ノードの状態。"], settings:["設定", "操作キーと、国別IPデータの設定。"], topology:["ネットワーク構成", "開発NWの接続とアドレスの参考情報。"]};
  const page = Object.hasOwn(pages, location.hash.slice(1)) ? location.hash.slice(1) : "monitor";
  for (const element of document.querySelectorAll("[data-page]")) element.hidden = element.dataset.page !== page;
  for (const link of document.querySelectorAll("[data-nav]")) {
    link.classList.toggle("active", link.dataset.nav === page);
    if (link.dataset.nav === page) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  $("page-title").textContent = pages[page][0]; $("page-description").textContent = pages[page][1];
}
function renderGeoip() {
  const data = snapshot?.geoip;
  $("geoip-status").textContent = data?.available ? "利用可能" : "未導入・利用不可";
  $("geoip-status").className = "badge " + (data?.available ? "good" : "warn");
  $("geoip-source").textContent = data?.source || "DB-IP Lite";
  $("geoip-edition").textContent = data?.edition || "—";
  $("geoip-updated").textContent = data?.updated_at ? new Date(data.updated_at).toLocaleString("ja-JP", {hour12:false}) : "—";
  $("geoip-message").textContent = data?.error || (data?.available ? "ローカルのデータを使って国を表示しています。" : "国別IPデータを導入すると公開IPの国を表示できます。LAN内と予約アドレスはデータ未導入でも区別します。");
}
function endpoint(ip, port) { return ip + (port === null || port === undefined || port === "" ? "" : ":" + port); }
function clock(value) { return value ? new Date(value).toLocaleTimeString("ja-JP", {hour12:false}) : "—"; }
function bytes(value) {
  if (value === null || value === undefined) return "—";
  for (const unit of ["B", "KB", "MB", "GB"]) { if (value < 1024 || unit === "GB") return value.toFixed(unit === "B" ? 0 : 1) + " " + unit; value /= 1024; }
}
function cell(row, value, className = "") {
  const td = document.createElement("td"); td.textContent = value; td.className = className; row.append(td); return td;
}
function renderControl() {
  const known = control && control.available && !control.error;
  $("gate-state").textContent = known ? control.enabled ? "ON · 接続を許可" : "OFF · 遮断中" : "状態不明";
  $("gate-state").className = "gate-state " + (known ? control.enabled ? "online" : "offline" : "unknown");
  const hasKey = controlDemo || activeToken.length > 0;
  $("internet-on").disabled = !known || controlBusy || control.enabled || !hasKey;
  $("internet-off").disabled = controlBusy || !hasKey || (known && !control.enabled);
  $("key-link").hidden = hasKey;
  $("key-status").textContent = controlDemo ? "デモ・キー不要" : activeToken ? "設定済み" : "未設定";
  $("key-status").className = "badge " + (hasKey ? "good" : "warn");
  $("nodes").replaceChildren();
  for (const node of control?.nodes || []) {
    const row = document.createElement("div"); row.className = "node-row";
    const identity = document.createElement("div"), title = document.createElement("strong"), ip = document.createElement("code");
    title.textContent = node.name; ip.textContent = node.ip; identity.append(title, ip);
    const detail = document.createElement("div"), status = document.createElement("span"), last = document.createElement("small");
    status.className = "badge " + (node.state === "up" ? "good" : "warn");
    status.textContent = node.state === "up" ? "正常" + (node.latency_ms === null ? "" : " · " + node.latency_ms + " ms") : node.state === "no_reply" ? "応答なし" : "確認不能";
    const age = Date.now() - new Date(node.checked_at).getTime();
    if (!control.available || age > 30000) { status.textContent = !control.available ? "状態未確認" : "監視データが古い"; status.className = "badge warn"; }
    last.textContent = "確認 " + clock(node.checked_at) + " / 最終正常 " + clock(node.last_success);
    detail.append(status, last); row.append(identity, detail); $("nodes").append(row);
  }
  if (!(control?.nodes || []).length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = "監視データはまだありません。"; $("nodes").append(p); }
  if (snapshot) snapshot.control = control;
}
async function refreshControl() {
  const epoch = controlEpoch;
  try {
    const response = await fetch("/api/control", {cache:"no-store", signal:AbortSignal.timeout(8000)});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || "状態取得失敗");
    if (epoch !== controlEpoch || controlBusy) return;
    control = data.control; controlDemo = data.demo; renderControl();
    $("control-message").textContent = controlDemo ? "デモ操作です。実際の通信設定は変更しません。" : control.error || "再起動時はOFF。ONは画面を閉じても維持され、制御停止時は最大20秒でOFFになります。";
  } catch (e) {
    if (epoch !== controlEpoch || controlBusy) return;
    control = {available:false, nodes:control?.nodes || [], error:e.message}; renderControl();
    $("control-message").textContent = "通信状態を確認できません。" + e.message;
  } finally { setTimeout(refreshControl, 5000); }
}
async function setInternet(enabled) {
  if (controlBusy) return;
  controlBusy = true; controlEpoch++; renderControl(); $("control-message").textContent = "通信状態を変更しています…";
  try {
    const response = await fetch("/api/internet", {method:"POST", headers:{"Content-Type":"application/json", "Authorization":"Bearer " + activeToken}, body:JSON.stringify({enabled}), signal:AbortSignal.timeout(10000)});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || "変更失敗");
    control = data.control; controlDemo = data.demo;
    $("control-message").textContent = controlDemo ? "デモ表示を" + (enabled ? "ON" : "OFF") + "にしました。実機は変更していません。" : enabled ? "ONを確認しました。元のFirewallで許可した外向き通信が可能です。" : "OFFを確認しました。既存の外部通信も遮断しています。";
  } catch (e) {
    // A timed-out response does not establish whether the change took effect.
    control = {available:false, nodes:control?.nodes || [], error:e.message};
    $("control-message").textContent = "変更結果は未確認です。" + e.message;
  } finally { controlBusy = false; controlEpoch++; renderControl(); }
}
function render() {
  if (!snapshot) return;
  const ai = snapshot.flows.filter(f => f.scope === "AI");
  $("ai-count").textContent = snapshot.collector_ok ? ai.length : "—";
  $("dest-count").textContent = snapshot.collector_ok ? new Set(ai.map(f => f.dst)).size : "—";
  $("tcp-count").textContent = snapshot.collector_ok ? ai.filter(f => f.proto === "TCP" && f.state === "ESTABLISHED").length : "—";
  $("drop-count").textContent = snapshot.drops.length;
  $("demo").hidden = !snapshot.demo;
  const bad = snapshot.stale || !snapshot.collector_ok || snapshot.errors.length;
  $("health").textContent = snapshot.stale ? "データ更新が停止" : bad ? "一部の収集に失敗" : snapshot.demo ? "デモデータ表示中" : "自動更新中 · 5秒";
  $("live-dot").classList.toggle("bad", Boolean(bad));
  $("updated").textContent = "最終収集 " + clock(snapshot.updated_at);
  const notices = [...snapshot.errors];
  if (snapshot.stale) notices.unshift("収集データが古くなっています。collectorサービスを確認してください。");
  if (snapshot.demo) notices.unshift("デモ表示です。表示中の通信は実機の通信ではありません。");
  $("notice").hidden = !notices.length; $("notice").textContent = notices.join("\n");
  renderGeoip();
  const search = $("search").value.toLowerCase(), scope = $("scope").value, proto = $("proto").value;
  const rows = snapshot[tab].filter(f => (scope === "all" || (scope === "related" ? ["AI", "MANAGEMENT"].includes(f.scope) : f.scope === scope)) && (proto === "all" || f.proto === proto) &&
    [f.src, f.dst, f.sport, f.dport, f.proto, f.service, f.state, f.chain, direction(f), countryText(f.src_country), countryText(f.dst_country)].join(" ").toLowerCase().includes(search));
  $("row-count").textContent = rows.length + " 件";
  $("table-head").replaceChildren(); const header = document.createElement("tr");
  for (const title of heads[tab]) { const th = document.createElement("th"); th.textContent = title; header.append(th); } $("table-head").append(header);
  $("rows").replaceChildren();
  for (const f of rows) {
    const row = document.createElement("tr");
    cell(row, direction(f));
    const srcCell = cell(row, endpoint(f.src, f.sport), "mono"), srcCountry = document.createElement("small");
    srcCountry.className = "endpoint-country"; srcCountry.textContent = countryText(f.src_country); srcCell.append(srcCountry);
    cell(row, endpoint(f.dst, f.dport), "mono");
    cell(row, countryText(f.dst_country), "country");
    cell(row, tab === "drops" ? f.proto : f.service + " / " + f.proto);
    const status = tab === "drops" ? f.chain + " DROP" : tab === "history" ? f.ended_at ? "観測終了" : "追跡中" : f.state;
    const td = cell(row, ""), badge = document.createElement("span"); badge.textContent = status;
    badge.className = "badge " + (tab === "drops" || status === "UNREPLIED" ? "warn" : status === "ESTABLISHED" ? "good" : ""); td.append(badge);
    cell(row, tab === "drops" ? (f.in || "—") + " → " + (f.out || "—") : bytes(f.tx_bytes) + " / " + bytes(f.rx_bytes), "mono");
    cell(row, clock(tab === "drops" ? f.time : tab === "history" ? f.last_seen : f.first_seen), "mono"); $("rows").append(row);
  }
  if (!rows.length) { const tr = document.createElement("tr"), td = cell(tr, "該当する通信はありません", "empty"); td.colSpan = 8; $("rows").append(tr); }
  $("table-note").textContent = tab === "drops"
    ? "直近5分・最大200件。既存FWのログに記録された拒否のみ表示します。レート制限やログのないルールによる拒否は含みません。"
    : tab === "history"
    ? "約5秒ごとの観測履歴から直近500件を表示。終了済みの保存は24時間・全体目安2万件。短い通信は見逃す場合があります。観測終了はTCP切断時刻ではありません。"
    : "TACOを通る通信を両方向の開始元で表示。LAN → AIの管理SSHも含みます。送受信量は開始元を基準にした累計、国はIPからの概算です。";
}
async function refresh() {
  try {
    const response = await fetch("/api/state", {cache:"no-store", signal:AbortSignal.timeout(8000)});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || "収集データ取得失敗"); snapshot = data; render();
  } catch (e) {
    $("health").textContent = "データ取得に失敗"; $("live-dot").classList.add("bad");
    $("notice").hidden = false; $("notice").textContent = "表示内容は最新ではありません。" + e.message;
  } finally { setTimeout(refresh, 5000); }
}
for (const name of Object.keys(heads)) $("tab-" + name).addEventListener("click", () => {
  tab = name;
  for (const other of Object.keys(heads)) { $("tab-" + other).classList.toggle("selected", other === tab); $("tab-" + other).setAttribute("aria-selected", String(other === tab)); }
  render();
});
for (const id of ["search", "scope", "proto"]) $(id).addEventListener("input", render);
$("export").addEventListener("click", () => {
  if (!snapshot) return; const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], {type:"application/json"}));
  const a = document.createElement("a"); a.href = url; a.download = "taco-sessions-" + new Date().toISOString().replace(/[:.]/g,"-") + ".json"; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
});
refresh();
$("key-form").addEventListener("submit", event => {
  event.preventDefault();
  const token = $("control-token").value.trim();
  if (!token) { $("key-message").textContent = "操作キーを入力してください。"; return; }
  activeToken = token; $("control-token").value = "";
  $("key-message").textContent = "この画面に設定しました。メニュー移動後も使用できます。ページ再読み込みで消えます。";
  renderControl();
});
$("clear-key").addEventListener("click", () => { activeToken = ""; $("control-token").value = ""; $("key-message").textContent = "操作キーを消去しました。"; renderControl(); });
$("internet-on").addEventListener("click", () => setInternet(true));
$("internet-off").addEventListener("click", () => setInternet(false));
window.addEventListener("hashchange", navigate);
navigate();
refreshControl();
