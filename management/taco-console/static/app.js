"use strict";
let snapshot = null, tab = "flows";
let control = null, controlBusy = false, controlDemo = false, controlEpoch = 0;
let activeToken = "", tokenSaved = false;
const tokenPreference = new window.TokenPreference();
const $ = id => document.getElementById(id);
let sessionView = "map";
const atlas = new window.SessionAtlas({canvas:$("atlas-canvas"), list:$("atlas-routes"),
  detail:$("atlas-detail"), summary:$("atlas-summary"), onInspect:address => {
    $("search").value = address; setSessionView("table"); render();
  },onSelectionChange:key=>{$("atlas-clear").disabled=!key;}});
$("atlas-pause").setAttribute("aria-pressed", String(atlas.paused));
$("atlas-pause").textContent = atlas.paused ? "動 / 演出再開" : "止 / 演出停止";
function setSessionView(view) {
  const page=view==="map" ? "monitor" : "sessions";
  if(location.hash.slice(1)===page)navigate();else location.hash=page;
}
function resetAtlasView() {
  $("search").value="";$("scope").value="related";$("proto").value="all";tab="flows";
  for(const name of Object.keys(heads)){$("tab-"+name).classList.toggle("selected",name===tab);$("tab-"+name).setAttribute("aria-selected",String(name===tab));}
  atlas.clearSelection();atlas.setPaused(atlas.initialPaused);
  $("atlas-pause").setAttribute("aria-pressed",String(atlas.paused));$("atlas-pause").textContent=atlas.paused ? "動 / 演出再開" : "止 / 演出停止";
  atlas.resetPosition();
  setSessionView("map");render();
}
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
  return flow.scope === "MANAGEMENT" ? taco ? "LAN → Radxa" : "LAN → AI" : flow.scope === "TACO" ? "Radxa発信" : taco ? "AI → Radxa" : "AI → 上流";
}
function navigate() {
  const pages = {monitor:"広域通信監視",sessions:"通信記録一覧",settings:"環境設定",topology:"構成情報"};
  const page = Object.hasOwn(pages, location.hash.slice(1)) ? location.hash.slice(1) : "monitor";
  for (const element of document.querySelectorAll("[data-page]")) element.hidden = element.dataset.page !== page;
  for (const link of document.querySelectorAll("[data-nav]")) {
    link.classList.toggle("active", link.dataset.nav === page);
    if (link.dataset.nav === page) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  $("page-title").textContent = pages[page];
  document.body.classList.toggle("monitor-mode",page==="monitor");
  document.body.classList.toggle("workspace-mode",page==="monitor" || page==="sessions");
  if(page==="monitor" || page==="sessions"){
    sessionView=page==="monitor" ? "map" : "table";
    $(page==="monitor" ? "map-controls" : "list-controls").append($("session-controls"));
    for(const name of ["map","table"]){$("view-"+name).classList.toggle("selected",sessionView===name);$("view-"+name).setAttribute("aria-pressed",String(sessionView===name));}
  }
  atlas.setActive(page === "monitor");
  render();
}
function renderGeoip() {
  const data = snapshot?.geoip;
  $("geoip-status").textContent = data?.available ? "有" : "無";
  $("geoip-status").title=data?.available ? "国別IPデータを利用可能" : "国別IPデータが未導入または利用不可";
  $("geoip-status").setAttribute("aria-label",$("geoip-status").title);
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
  const state = known ? control.enabled ? "online" : "offline" : "unknown";
  $("gate-panel").dataset.state=state;
  $("gate-panel").setAttribute("aria-busy",String(controlBusy));
  $("gate-state").textContent = known ? control.enabled ? "開" : "閉" : "不明";
  $("gate-caption").textContent = known ? control.enabled ? "インターネット接続を許可中" : "インターネット接続を遮断中" : "現在の接続状態を確認できません";
  $("gate-state").className = "gate-state " + state;
  const hasKey = controlDemo || activeToken.length > 0;
  $("internet-toggle").disabled=controlBusy || !hasKey;
  $("internet-toggle").setAttribute("aria-checked",String(Boolean(known && control.enabled)));
  $("internet-toggle").setAttribute("aria-label",known ? "AIセグメントの対外接続" : "AIセグメントの対外接続・状態不明（操作すると閉じます）");
  $("internet-toggle").title=controlBusy ? "変更中…" : known ? control.enabled ? "接続を閉じる" : "接続を開く" : "接続を閉じる（現在の状態は不明）";
  $("key-link").hidden = hasKey;
  $("key-status").textContent = controlDemo ? "試" : activeToken ? "済" : "未";
  $("key-status").title=controlDemo ? "試験表示・キー不要" : activeToken ? tokenSaved ? "ブラウザーに保存済み" : "この画面に設定済み" : "操作キー未設定";
  $("key-status").setAttribute("aria-label",$("key-status").title);
  $("key-status").className = "badge " + (hasKey ? "good" : "warn");
  $("nodes").replaceChildren();
  for (const node of control?.nodes || []) {
    const row = document.createElement("div"); row.className = "node-row";
    const identity = document.createElement("div"), title = document.createElement("strong"), ip = document.createElement("code");
    title.textContent = ({openwrt:"Netgear",taco:"Radxa",spark:"Sun SPARC",dgx:"Sun SPARC"})[node.id] || ({OpenWrt:"Netgear",TACO:"Radxa","DGX Spark":"Sun SPARC"})[node.name] || node.name; ip.textContent = node.ip; identity.append(title, ip);
    const status = document.createElement("span"), last = document.createElement("small");
    status.className = "node-state " + (node.state === "up" ? "good" : "warn");
    status.textContent = node.state === "up" ? "正" : node.state === "no_reply" ? "断" : "不";
    let description=node.state==="up" ? "正常" : node.state==="no_reply" ? "応答なし" : "確認不能";
    const age = Date.now() - new Date(node.checked_at).getTime();
    if (!control.available || age > 30000) { status.textContent = !control.available ? "不" : "古"; status.className = "node-state warn";description=!control.available ? "状態未確認" : "監視データが古い"; }
    status.title=description;status.setAttribute("aria-label",description);
    last.textContent = (node.latency_ms == null ? "LOCAL" : node.latency_ms+" ms") + " / " + clock(node.checked_at);
    last.title="最終正常 "+clock(node.last_success);identity.append(last);row.append(status,identity);$("nodes").append(row);
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
    $("control-message").textContent = controlDemo ? "試験表示・実機操作なし" : control.error || "管理SSH・Radxa自身の通信は維持";
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
    $("control-message").textContent = controlDemo ? "試験 / " + (enabled ? "開" : "閉") : enabled ? "対外接続を許可しました" : "既存の外部通信も遮断しました";
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
  $("notice").hidden = !notices.length; $("notice").textContent = notices.join("\n");
  renderGeoip();
  const search = $("search").value.toLowerCase(), scope = $("scope").value, proto = $("proto").value;
  const matches = f => (scope === "all" || (scope === "related" ? ["AI", "MANAGEMENT"].includes(f.scope) : f.scope === scope)) && (proto === "all" || f.proto === proto) &&
    [f.src, f.dst, f.sport, f.dport, f.proto, f.service, f.state, f.chain, direction(f), countryText(f.src_country), countryText(f.dst_country)].join(" ").toLowerCase().includes(search);
  const rows = snapshot[tab].filter(matches);
  $("row-count").textContent = rows.length + " 件";
  atlas.update(rows, {kind:tab, stale:snapshot.stale || !snapshot.collector_ok});
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
    : "Radxaを通る通信を両方向の開始元で表示。LAN → AIの管理SSHも含みます。送受信量は開始元を基準にした累計、国はIPからの概算です。";
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
function saveTokenPreference(remember) {
  const result=tokenPreference.save(activeToken,remember);
  tokenSaved=result.saved;
  $("key-message").textContent=result.error
    ? "キーはこの画面で使用できますが、ブラウザーの保存・削除に失敗しました。ブラウザーのサイトデータ設定を確認してください。"
    : result.saved ? "このブラウザーに保存しました。次回は自動で読み込みます。"
    : "キーはこの画面だけで使用します。保存済みのキーは削除しました。";
  renderControl();
  return result;
}
const restoredToken=tokenPreference.read();
activeToken=restoredToken.token;tokenSaved=Boolean(activeToken);
$("remember-token").checked=tokenSaved;
if(tokenSaved)$("key-message").textContent="保存済みの操作キーを読み込みました。";
$("key-form").addEventListener("submit", event => {
  event.preventDefault();
  const token = $("control-token").value.trim() || activeToken;
  if (!token) { $("key-message").textContent = "操作キーを入力してください。"; return; }
  activeToken = token; $("control-token").value = "";
  saveTokenPreference($("remember-token").checked);
});
$("remember-token").addEventListener("change",()=>{
  if(!$("remember-token").checked)saveTokenPreference(false);
});
$("clear-key").addEventListener("click", () => {
  activeToken="";$("control-token").value="";$("remember-token").checked=false;
  const result=saveTokenPreference(false);
  if(!result.error)$("key-message").textContent="操作キーを消去しました。";
});
$("internet-toggle").addEventListener("click", () => setInternet(Boolean(control?.available && !control.error && !control.enabled)));
$("view-map").addEventListener("click", () => setSessionView("map"));
$("view-table").addEventListener("click", () => setSessionView("table"));
$("atlas-pause").addEventListener("click", () => {
  atlas.setPaused(!atlas.paused); $("atlas-pause").setAttribute("aria-pressed", String(atlas.paused));
  $("atlas-pause").textContent = atlas.paused ? "動 / 演出再開" : "止 / 演出停止";
});
$("atlas-clear").addEventListener("click",()=>atlas.clearSelection());
$("atlas-reset").addEventListener("click",resetAtlasView);
window.addEventListener("hashchange", navigate);
navigate();
refreshControl();

function applyTheme(value) {
  const name = Object.hasOwn(window.SessionAtlas.themes,value) ? value : "amber";
  document.documentElement.dataset.theme=name;
  $("color-scheme").value=name;
  atlas.setTheme(name);
  return name;
}
let savedTheme="amber";
try { savedTheme=localStorage.getItem("taco-color-scheme") || "amber"; } catch (_) {}
applyTheme(savedTheme);
$("color-scheme").addEventListener("change",()=>{
  const name=applyTheme($("color-scheme").value);
  try { localStorage.setItem("taco-color-scheme",name);$("theme-message").textContent="配色をこのブラウザーに保存しました。"; }
  catch (_) { $("theme-message").textContent="配色を変更しました。ブラウザーに保存できないため、再読み込みすると元に戻ります。"; }
});
