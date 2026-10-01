/* 消防通道临停占道管理 — 前端(无框架,无构建) */
(() => {
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch { /* ignore */ }
  if (!res.ok) throw Object.assign(new Error(data?.error || `请求失败(${res.status})`), { status: res.status });
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (ms) => ms ? new Date(ms).toLocaleString("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
const fmtTimeFull = (ms) => ms ? new Date(ms).toLocaleString("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";

const STATUS_CN = {
  open: "待接单", patrol: "巡查中", tow_requested: "待升级审批",
  escalated: "已批准拖移", departed: "已驶离待复核", closed: "已结案",
};
const CHANNEL_CN = { sms: "短信", phone: "电话", onsite: "现场", notice: "书面/更正" };
const RESULT_CN = {
  reached: "已联系上", promised: "承诺驶离", no_answer: "无人接听",
  wrong_number: "号码有误/识别错误", refused: "拒绝驶离", failed: "发送失败",
};
const DEPART_CN = { self_drive: "车主自行驶离", towed: "依法拖离", emergency_release: "救援结束放行" };
const BLOCKING = new Set(["open", "patrol", "tow_requested", "escalated"]);

const state = {
  user: null,
  map: null,
  events: [],
  statusFilter: "",
  timer: null,
  photoBuf: null, // 上报弹窗里待提交的照片 dataURL
};

// ---------- Toast ----------
let toastTimer = null;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

// ---------- 认证 ----------
async function boot() {
  try {
    const { user } = await api("/api/me");
    enterApp(user);
  } catch {
    showLogin();
  }
}

function showLogin() {
  $("#login-view").classList.remove("hidden");
  $("#app-view").classList.add("hidden");
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    const { user } = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username: fd.get("username"), password: fd.get("password") }),
    });
    enterApp(user);
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.reload();
});

function enterApp(user) {
  state.user = user;
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  const roleCN = { resident: "住户", guard: "保安", supervisor: "值班负责人" }[user.role];
  $("#user-label").textContent = `${user.display_name} · ${roleCN}`;
  $("#report-fab").classList.toggle("hidden", user.role === "resident");
  $("#tab-sim").classList.toggle("hidden", user.role !== "supervisor");
  $("#tab-stats").classList.toggle("hidden", user.role === "resident");
  $("#tab-review").classList.toggle("hidden", user.role === "resident");
  switchTab(user.role === "resident" ? "notifications" : "map");
  refreshAll();
  clearInterval(state.timer);
  state.timer = setInterval(refreshAll, 15000);
}

// ---------- Tabs ----------
$$("#tabs .tab").forEach(btn => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));
function switchTab(name) {
  $$("#tabs .tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab-panel").forEach(p => p.classList.add("hidden"));
  $(`#tab-${name}`).classList.remove("hidden");
  if (name === "map") renderMap();
  if (name === "events") renderEventList();
  if (name === "review") renderReviewList();
  if (name === "notifications") renderNotifications();
  if (name === "stats") renderStats();
  if (name === "sim") loadSimConfig();
}

// ---------- 数据刷新 ----------
async function refreshAll() {
  try {
    const [mapData, evData, notifData] = await Promise.all([
      api("/api/map"),
      api(state.statusFilter ? `/api/events?status=${state.statusFilter}` : "/api/events"),
      state.user ? api("/api/notifications") : Promise.resolve({ notifications: [] }),
    ]);
    state.map = mapData;
    state.events = evData.events;
    state.notifications = notifData.notifications;
    updateBadges();
    if (!$("#tab-map").classList.contains("hidden")) renderMap();
    if (!$("#tab-events").classList.contains("hidden")) renderEventList();
    if (!$("#tab-review").classList.contains("hidden")) renderReviewList();
    if (!$("#tab-notifications").classList.contains("hidden")) renderNotifications();
  } catch (err) {
    if (err.status === 401) showLogin();
  }
}

function updateBadges() {
  const unread = (state.notifications || []).filter(n => !n.read_at).length;
  const nb = $("#notif-badge");
  nb.textContent = unread;
  nb.classList.toggle("hidden", unread === 0);
  const pending = state.events.filter(e => e.status === "departed");
  const rb = $("#review-badge");
  rb.textContent = pending.length;
  rb.classList.toggle("hidden", pending.length === 0 || state.user.role === "resident");
}

// ---------- 占用时长 ----------
function durationOf(e, now = Date.now()) {
  const end = e.status === "closed" ? e.closed_at
    : e.status === "departed" ? e.departed_at : now;
  let ms = Math.max(0, end - e.found_at);
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60), rm = m % 60;
  if (h < 48) return `${h} 小时 ${rm} 分`;
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`;
}

// ---------- 地图 ----------
function renderMap() {
  const svg = $("#map-svg");
  if (!state.map) { svg.innerHTML = ""; return; }
  const { buildings, zones, events } = state.map;
  const byZone = {};
  events.forEach(e => { (byZone[e.zone_id] ||= []).push(e); });
  const zoneById = Object.fromEntries(zones.map(z => [z.id, z]));

  let html = `<rect x="0" y="0" width="1000" height="640" fill="var(--surface)"/>`;

  // 消防车道连接线(北/南两条环形通道),阻断段标红
  const north = zones.filter(z => z.kind === "exit" && z.py < 300).sort((a, b) => a.px - b.px);
  const south = zones.filter(z => z.kind === "exit" && z.py > 300).sort((a, b) => a.px - b.px);
  const drawLinks = (pts) => {
    for (let i = 0; i < pts.length - 1; i++) {
      const blocked = (byZone[pts[i].id] || []).some(e => !e.redacted && BLOCKING.has(e.status))
        || (byZone[pts[i + 1].id] || []).some(e => !e.redacted && BLOCKING.has(e.status));
      html += `<line class="lane-link ${blocked ? "blocked" : ""}" x1="${pts[i].px}" y1="${pts[i].py}"
        x2="${pts[i + 1].px}" y2="${pts[i + 1].py}"/>`;
    }
  };
  drawLinks(north);
  drawLinks(south);
  // 南北连接线
  [north[0], north[north.length - 1]].forEach((n, i) => {
    const s = i === 0 ? south[0] : south[south.length - 1];
    const blocked = (byZone[n.id] || []).some(e => BLOCKING.has(e.status))
      || (byZone[s.id] || []).some(e => BLOCKING.has(e.status));
    html += `<line class="lane-link ${blocked ? "blocked" : ""}" x1="${n.px}" y1="${n.py}"
      x2="${s.px}" y2="${s.py}"/>`;
  });

  // 楼栋
  buildings.forEach(b => {
    html += `<rect class="building-rect" x="${b.px - 95}" y="${b.py - 70}" width="190" height="140"/>
      <text class="building-label" x="${b.px}" y="${b.py + 6}">${esc(b.name)}</text>`;
  });

  // 区域锚点
  zones.forEach(z => {
    const evs = byZone[z.id] || [];
    const blocking = evs.some(e => BLOCKING.has(e.status));
    const reviewing = !blocking && evs.some(e => !e.redacted && e.status === "departed");
    const cls = z.kind === "exit" ? "zone-circle" : "zone-circle lane";
    const r = z.rx || 26;
    html += `<g class="map-zone" data-zone="${z.id}">`;
    if (blocking) {
      html += `<circle class="pulse" cx="${z.px}" cy="${z.py}" r="${r + 6}"
        fill="var(--critical)" opacity=".25"/>`;
    }
    html += `<circle class="${cls} ${blocking ? "blocked" : reviewing ? "review-pending" : ""}"
      cx="${z.px}" cy="${z.py}" r="${r}"/>`;
    const labelY = z.py + 4;
    html += `<text class="zone-label" x="${z.px}" y="${labelY}"
      style="${blocking ? "fill:#fff" : ""}">${z.kind === "exit" ? "🚪" : "⛡"}</text>`;
    const side = z.py < 300 ? -1 : 1;
    html += `<text class="zone-sub" x="${z.px}" y="${z.py + side * (r + 16)}">${esc(z.name)}</text>`;
    if (reviewing) {
      html += `<text class="zone-sub" x="${z.px}" y="${z.py + side * (r + 32)}"
        style="fill:var(--warning);font-weight:600">待复核</text>`;
    }
    html += `</g>`;
  });

  // 事件图钉(红色仅给正在阻断的)
  events.forEach((e, i) => {
    const z = zoneById[e.zone_id];
    if (!z) return;
    const dx = (i % 3 - 1) * 30, dy = (i % 2) * 26 - 13;
    const isBlocking = BLOCKING.has(e.status);
    const pinCls = isBlocking ? "pin-blocking" : "pin-review";
    const glyph = e.redacted ? "!" : e.status === "departed" ? "✓" : "占";
    const title = e.redacted
      ? `${z.name}:消防通道被外部车辆占用(跨楼栋,仅提示)`
      : `${e.plate} · ${STATUS_CN[e.status]} · 占用 ${durationOf(e)}`;
    html += `<g class="event-pin ${pinCls} ${e.redacted ? "redacted" : ""}" data-event="${e.id}"
      transform="translate(${z.px + r0(z) + dx},${z.py - r0(z) + dy})">
      <title>${esc(title)}</title>
      <circle cx="0" cy="0" r="15"/>
      <text x="0" y="4">${glyph}</text></g>`;
  });

  function r0(z) { return z.rx || 26; }

  svg.innerHTML = html;

  $$(".event-pin", svg).forEach(g => g.addEventListener("click", () => {
    const id = Number(g.dataset.event);
    const e = events.find(x => x.id === id);
    if (e?.redacted) {
      toast("跨楼栋事件:出于范围控制仅提示占用,详情请联系该楼栋物业", "");
      return;
    }
    openEvent(id);
  }));
  $$(".map-zone", svg).forEach(g => g.addEventListener("click", () => {
    const z = zoneById[Number(g.dataset.zone)];
    const evs = byZone[z.id] || [];
    const visible = evs.find(e => !e.redacted);
    if (visible) { openEvent(visible.id); return; }
    if (state.user.role === "resident") { toast("该区域当前无您车辆相关的事件"); return; }
    // 全为跨楼栋红色提示:保安不能上报他栋区域
    if (evs.some(e => e.redacted) && z.building_id !== state.user.building_id
        && state.user.role !== "supervisor") {
      toast("该事件属于其他楼栋,仅提示通道阻断;请联系该楼栋物业处置");
      return;
    }
    openReportModal(z.id);
  }));
}

// ---------- 状态筛选 ----------
function initFilters() {
  const sel = $("#filter-status");
  Object.entries(STATUS_CN).forEach(([k, v]) => {
    const o = document.createElement("option");
    o.value = k; o.textContent = v;
    sel.appendChild(o);
  });
  sel.addEventListener("change", () => {
    state.statusFilter = sel.value;
    refreshAll().then(renderEventList);
  });
}

// ---------- 事件卡片 ----------
function eventCard(e) {
  const blocking = BLOCKING.has(e.status);
  const cls = blocking ? "blocking" : e.status === "departed" ? "departed"
    : e.status === "closed" ? "closed" : "";
  const handler = e.accepted_by_name
    ? `实际处置人:${e.accepted_by_name}`
    : "尚无保安接单";
  const reviewLine = e.status === "closed" && e.reviewed_by_name
    ? `<span>复核人:<b>${esc(e.reviewed_by_name)}</b></span>` : "";
  const tags = [
    e.is_emergency ? `<span class="tag-emergency">🚑 紧急救援占道</span>` : "",
    e.duplicate_count > 0 ? `<span class="tag-merge">已合并 ${e.duplicate_count} 次重复上报</span>` : "",
    e.original_plate !== e.plate
      ? `<span class="tag-merge" title="车牌识别已更正">原识别 ${esc(e.original_plate)}</span>` : "",
  ].join("");
  return `<div class="ev-card ${cls} ${e.is_emergency ? "emergency" : ""}" data-event="${e.id}">
    <div class="ev-top">
      <span class="plate">${esc(e.plate)}</span>
      <span class="status-chip chip-${e.status}">${STATUS_CN[e.status]}</span>
    </div>
    <div class="zone-line">${esc(e.building_name || "")} · ${esc(e.zone_name || "")}</div>
    <div>${tags}</div>
    <div class="ev-meta">
      <span class="duration">⏱ 占用时长:<b>${durationOf(e)}</b></span>
      <span>发现于 ${fmtTime(e.found_at)}</span>
      <span>${handler}</span>
      ${reviewLine}
    </div>
  </div>`;
}

function renderEventList() {
  const hint = {
    resident: "仅显示登记在您名下车辆的事件。",
    guard: "仅显示您所在楼栋的事件;其他楼栋仅在平面图上提示占用。",
    supervisor: "全部楼栋事件。",
  }[state.user.role];
  $("#events-scope-hint").textContent = hint;
  const list = state.events;
  $("#event-list").innerHTML = list.length
    ? list.map(eventCard).join("")
    : `<p class="muted">当前没有符合条件的事件。</p>`;
  $$("#event-list .ev-card").forEach(c =>
    c.addEventListener("click", () => openEvent(Number(c.dataset.event))));
}

function renderReviewList() {
  const pending = state.events.filter(e => e.status === "departed");
  const box = $("#review-list");
  if (!pending.length) {
    box.innerHTML = `<p class="muted">没有待复核事件。通道恢复复核通过后事件即结案封闭。</p>`;
    return;
  }
  box.innerHTML = pending.map(e => {
    const self = e.departed_by === state.user.id;
    return `<div class="ev-card departed" data-event="${e.id}">
      <div class="ev-top">
        <span class="plate">${esc(e.plate)}</span>
        <span class="status-chip chip-departed">已驶离待复核</span>
      </div>
      <div class="zone-line">${esc(e.building_name)} · ${esc(e.zone_name)}</div>
      <div class="ev-meta">
        <span>驶离方式:<b>${DEPART_CN[e.departure_reason] || "—"}</b></span>
        <span>驶离登记人:<b>${esc(e.departed_by_name || "—")}</b></span>
        <span class="duration">占道总时长:<b>${durationOf(e)}</b></span>
      </div>
      <div class="ev-actions">
        ${self
          ? `<button class="btn small" disabled title="驶离登记人与复核人必须分离">需由另一名保安复核</button>`
          : `<button class="btn primary small" data-review="${e.id}">我已现场确认,复核结案</button>`}
        <button class="btn ghost small" data-open="${e.id}">查看详情</button>
      </div>
    </div>`;
  }).join("");
  $$("#review-list [data-review]").forEach(b =>
    b.addEventListener("click", (ev) => { ev.stopPropagation(); reviewQuick(Number(b.dataset.review)); }));
  $$("#review-list [data-open]").forEach(b =>
    b.addEventListener("click", (ev) => { ev.stopPropagation(); openEvent(Number(b.dataset.open)); }));
  $$("#review-list .ev-card").forEach(c =>
    c.addEventListener("click", () => openEvent(Number(c.dataset.event))));
}

async function reviewQuick(eid) {
  const note = prompt("复核备注(确认消防通道已恢复畅通、无遗留障碍):", "通道已恢复,无障碍物");
  if (note === null) return;
  try {
    await api(`/api/events/${eid}/review`, { method: "POST", body: JSON.stringify({ note }) });
    toast("复核完成,事件已结案", "success");
    refreshAll();
  } catch (err) { toast(err.message, "error"); }
}

// ---------- 通知 ----------
async function renderNotifications() {
  const list = state.notifications || [];
  const box = $("#notif-list");
  if (!list.length) { box.innerHTML = `<p class="muted">暂无通知。</p>`; return; }
  box.innerHTML = list.map(n => `
    <div class="notif ${n.read_at ? "" : "unread"}" data-notif="${n.id}" data-event="${n.event_id}">
      <div>${esc(n.text)}</div>
      <time>${fmtTime(n.created_at)}</time>
    </div>`).join("");
  $$("#notif-list .notif").forEach(el => el.addEventListener("click", async () => {
    const nid = Number(el.dataset.notif);
    await api(`/api/notifications/${nid}/read`, { method: "POST" }).catch(() => {});
    // 住户只能看到自己车辆的事件,必然有权打开
    try { await openEvent(Number(el.dataset.event)); } catch { /* ignore */ }
    refreshAll();
  }));
}

// ---------- 上报弹窗 ----------
$("#report-fab").addEventListener("click", () => openReportModal());

function openReportModal(prefZone) {
  if (state.user.role === "resident" || !state.map) return;
  const sel = $("#report-form select[name=zone_id]");
  sel.innerHTML = "";
  state.map.zones
    .filter(z => state.user.role === "supervisor" || z.building_id === state.user.building_id)
    .forEach(z => {
      const o = document.createElement("option");
      o.value = z.id;
      o.textContent = `${z.kind === "exit" ? "消防出入口" : "消防车道"} · ${z.name}`;
      sel.appendChild(o);
    });
  if (prefZone) sel.value = String(prefZone);
  $("#report-form").reset();
  if (prefZone) sel.value = String(prefZone);
  state.photoBuf = null;
  $("#report-preview").classList.add("hidden");
  $("#lpr-hint").className = "hint hidden";
  $("#report-modal").classList.remove("hidden");
}

$$("[data-close]").forEach(b => b.addEventListener("click", () =>
  b.closest(".modal-mask").classList.add("hidden")));
$(".modal-mask") && $$(".modal-mask").forEach(m => m.addEventListener("click", (e) => {
  if (e.target === m) m.classList.add("hidden");
}));

$("#report-photo").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    state.photoBuf = rd.result;
    const pv = $("#report-preview");
    pv.querySelector("img").src = rd.result;
    pv.classList.remove("hidden");
  };
  rd.readAsDataURL(f);
});

$("#lpr-btn").addEventListener("click", async () => {
  const zoneId = Number($("#report-form select[name=zone_id]").value);
  const hintEl = $("#lpr-hint");
  try {
    const r = await api("/api/sim/recognize", {
      method: "POST", body: JSON.stringify({ zone_id: zoneId }),
    });
    $("#report-form input[name=plate]").value = r.plate;
    hintEl.classList.remove("hidden");
    if (r.sim_mode === "error") {
      hintEl.className = "hint warn";
      hintEl.textContent = `⚠ 识别器低置信度返回「${r.raw}」(${(r.confidence * 100).toFixed(0)}%)。`
        + "可能识别错误——提交前请现场核对,提交后也可在详情中更正(全程留痕)。";
    } else {
      hintEl.className = "hint";
      hintEl.textContent = `识别成功:${r.raw}(置信度 ${(r.confidence * 100).toFixed(0)}%)`;
    }
  } catch (err) { toast(err.message, "error"); }
});

$("#report-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const { merged } = await api("/api/events", {
      method: "POST",
      body: JSON.stringify({
        plate: fd.get("plate"),
        zone_id: Number(fd.get("zone_id")),
        is_emergency: fd.get("is_emergency") === "on",
        photo: state.photoBuf,
      }),
    });
    $("#report-modal").classList.add("hidden");
    toast(merged ? "同一车辆已有进行中的占用事件,已合并到原单(不重复开单)" : "上报成功,事件已生成",
      merged ? "" : "success");
    refreshAll();
  } catch (err) { toast(err.message, "error"); }
});

// ---------- 事件详情 ----------
async function openEvent(eid) {
  let d;
  try { d = await api(`/api/events/${eid}`); }
  catch (err) { toast(err.message, "error"); return; }
  state.currentDetail = d;
  $("#ev-title").textContent = `事件 #${d.id} · ${d.plate}`;
  const isStaff = state.user.role !== "resident";
  const photos = {
    report: d.photos.filter(p => p.kind === "report"),
    departure: d.photos.filter(p => p.kind === "departure"),
    review: d.photos.filter(p => p.kind === "review"),
  };
  const photoBlock = (title, arr) => arr.length ? `
    <h4>${title}</h4><div class="photos-row">${arr.map(p =>
      `<div class="photo-thumb"><a href="/photos/${esc(p.filename)}" target="_blank">
      <img src="/photos/${esc(p.filename)}" alt="${title}" loading="lazy"></a><span>${fmtTime(p.created_at)}</span></div>`
    ).join("")}</div>` : "";

  const el = $("#event-detail");
  el.innerHTML = `
    <div class="ev-top">
      <div>
        <span class="plate">${esc(d.plate)}</span>
        ${d.original_plate !== d.plate
          ? ` <span class="tag-merge">由 ${esc(d.original_plate)} 更正</span>` : ""}
        ${d.is_emergency ? ` <span class="tag-emergency">🚑 紧急救援占道</span>` : ""}
        ${d.duplicate_count ? ` <span class="tag-merge">已合并 ${d.duplicate_count} 次重复上报</span>` : ""}
      </div>
      <span class="status-chip chip-${d.status}">${STATUS_CN[d.status]}</span>
    </div>
    <div class="ev-meta" style="margin-top:8px">
      <span>区域:<b>${esc(d.building_name)} · ${esc(d.zone.name)}</b></span>
      <span>发现时间:${fmtTimeFull(d.found_at)}</span>
      <span>上报人:${esc(d.reported_by_name || "—")}</span>
      <span>处置保安:${esc(d.accepted_by_name || "尚未接单")}</span>
      ${d.reviewed_by_name ? `<span>复核人:${esc(d.reviewed_by_name)}</span>` : ""}
      <span class="duration">占用时长:<b>${durationOf(d)}</b></span>
    </div>

    ${photoBlock("现场照片(上报)", photos.report)}
    ${photoBlock("驶离照片", photos.departure)}
    ${photoBlock("复核照片", photos.review)}

    ${isStaff ? staffActions(d) : `
      <div class="detail-section"><p class="muted">您是车辆相关住户,可查看本事件全过程;
      事件记录由物业人员维护,已结案事件封闭不可修改。</p></div>`}

    <div class="detail-section">
      <h4>联系记录</h4>
      ${d.contacts.length ? `<table class="contacts">
        <thead><tr><th>时间</th><th>渠道</th><th>结果</th><th>说明</th><th>登记人</th></tr></thead>
        <tbody>${d.contacts.map(c => `<tr>
          <td>${fmtTime(c.created_at)}</td>
          <td>${CHANNEL_CN[c.channel] || c.channel}</td>
          <td class="result-${c.result}">${RESULT_CN[c.result] || c.result}</td>
          <td>${esc(c.detail || "")}</td>
          <td>${esc(c.actor_name || "—")}</td></tr>`).join("")}</tbody></table>`
        : `<p class="muted">暂无联系记录。</p>`}
      ${isStaff && d.status !== "closed" ? contactForm(d) : ""}
    </div>

    <div class="detail-section">
      <h4>事件时间线(全程留痕)</h4>
      <ul class="timeline">${d.timeline.map(t => `
        <li class="kind-${t.kind}">
          <div class="tl-time">${fmtTimeFull(t.created_at)}</div>
          <div class="tl-text">${esc(t.detail || KIND_CN[t.kind] || t.kind)}
            <span class="tl-actor">— ${esc(t.actor_name || "系统")}</span></div>
        </li>`).join("")}</ul>
    </div>`;
  bindDetailActions(el, d);
  $("#event-modal").classList.remove("hidden");
}

const KIND_CN = {
  reported: "发现占道", emergency_report: "紧急救援占道上报", accepted: "接单巡查",
  contact: "新增联系记录", duplicate_report: "重复上报自动合并",
  plate_corrected: "车牌识别更正", tow_requested: "申请拖移", escalated: "批准升级拖移",
  tow_cancelled: "拖移取消", departed: "车辆驶离", reviewed: "复核结案",
};

function staffActions(d) {
  const u = state.user;
  const inScope = u.role === "supervisor" || true; // 详情接口已做范围过滤
  const buttons = [];
  if (d.status === "open") {
    buttons.push(`<button class="btn primary" data-act="accept">接单前往巡查</button>`);
  }
  if (BLOCKING.has(d.status)) {
    buttons.push(`<button class="btn" data-act="correct">更正识别车牌</button>`);
  }
  if (d.status === "patrol") {
    buttons.push(`<button class="btn danger" data-act="tow-request">申请升级拖移</button>`);
  }
  if (d.status === "tow_requested" && u.role === "supervisor") {
    buttons.push(`<button class="btn danger" data-act="escalate">值班负责人批准拖移</button>`);
  }
  if ((d.status === "tow_requested" || d.status === "escalated") && u.role === "supervisor") {
    buttons.push(`<button class="btn" data-act="tow-cancel">取消拖移</button>`);
  }
  if (BLOCKING.has(d.status)) {
    buttons.push(`<button class="btn" data-act="depart">登记车辆驶离</button>`);
  }
  if (d.status === "departed") {
    const self = d.departed_by === u.id;
    buttons.push(self
      ? `<button class="btn" disabled title="必须由另一名保安复核">驶离由您登记,需他人复核</button>`
      : `<button class="btn primary" data-act="review">现场复核通道恢复并结案</button>`);
  }
  if (d.status === "closed") {
    buttons.push(`<span class="muted">事件已于 ${fmtTimeFull(d.closed_at)} 结案封闭,记录不可修改。</span>`);
  }
  return `<div class="detail-section"><h4>处置操作</h4>
    <div class="ev-actions">${buttons.join("")}</div>
    <div id="action-slot"></div></div>`;
}

function contactForm() {
  return `<div id="contact-form" style="margin-top:12px;border-top:1px dashed var(--hairline);padding-top:12px">
    <div class="plate-row">
      <label class="grow">渠道
        <select name="c_channel">
          <option value="sms">短信(模拟网关)</option>
          <option value="phone">电话</option>
          <option value="onsite">现场告知</option>
          <option value="notice">书面通知</option>
        </select></label>
      <label class="grow">结果
        <select name="c_result">
          <option value="reached">已联系上</option>
          <option value="promised">承诺驶离</option>
          <option value="no_answer">无人接听</option>
          <option value="wrong_number">号码有误/识别错误</option>
          <option value="refused">拒绝驶离</option>
          <option value="failed">发送失败</option>
        </select></label>
    </div>
    <label>说明<input name="c_detail" placeholder="如:车主称 10 分钟内到场"></label>
    <label class="check"><input type="checkbox" name="c_sms" checked> 同时发送模拟短信(失败自动留痕)</label>
    <button class="btn primary small" data-act="contact-add">保存联系记录</button>
  </div>`;
}

function photoInput(id) {
  return `<label>现场/复核照片
    <input type="file" id="${id}" accept="image/*" capture="environment"></label>
  <div class="photo-preview hidden"><img alt="照片预览"></div>`;
}

function readPhoto(inputEl) {
  return new Promise((resolve) => {
    const f = inputEl?.files?.[0];
    if (!f) return resolve(null);
    const rd = new FileReader();
    rd.onload = () => resolve(rd.result);
    rd.readAsDataURL(f);
  });
}

function bindDetailActions(root, d) {
  const u = state.user;
  const post = async (act, body) => api(`/api/events/${d.id}/${act}`,
    { method: "POST", body: JSON.stringify(body) });
  const done = async (promise, ok) => {
    try { await promise; toast(ok, "success"); $("#event-modal").classList.add("hidden"); refreshAll(); }
    catch (err) { toast(err.message, "error"); }
  };

  root.querySelector('[data-act="contact-add"]')?.addEventListener("click", async () => {
    await done(post("contact", {
      channel: root.querySelector("[name=c_channel]").value,
      result: root.querySelector("[name=c_result]").value,
      detail: root.querySelector("[name=c_detail]").value,
      send_sms: root.querySelector("[name=c_sms]").checked,
    }), "联系记录已保存");
  });

  root.querySelector('[data-act="accept"]')?.addEventListener("click", () =>
    done(post("accept", {}), "已接单,请前往现场巡查"));

  root.querySelector('[data-act="correct"]')?.addEventListener("click", () => {
    const slot = root.querySelector("#action-slot");
    slot.innerHTML = `<label>正确车牌<input id="corr-plate" value="${esc(d.plate)}"></label>
      <label>更正原因<input id="corr-reason" placeholder="如:现场核对行驶证"></label>
      <button class="btn primary small" id="corr-ok">确认更正</button>`;
    slot.querySelector("#corr-ok").addEventListener("click", () =>
      done(post("correct-plate", {
        plate: slot.querySelector("#corr-plate").value,
        reason: slot.querySelector("#corr-reason").value,
      }), "车牌已更正并通知车主,全程留痕"));
  });

  root.querySelector('[data-act="tow-request"]')?.addEventListener("click", () => {
    const slot = root.querySelector("#action-slot");
    slot.innerHTML = `<label>申请理由(提交后由值班负责人审批)
      <textarea id="tow-reason">阻断消防通道,经联系后仍未驶离</textarea></label>
      <button class="btn danger small" id="tow-ok">提交拖移申请</button>`;
    slot.querySelector("#tow-ok").addEventListener("click", () =>
      done(post("tow-request", { reason: slot.querySelector("#tow-reason").value }),
        "拖移申请已提交,等待值班负责人升级决定"));
  });

  root.querySelector('[data-act="escalate"]')?.addEventListener("click", () => {
    const slot = root.querySelector("#action-slot");
    slot.innerHTML = `<div class="confirm-box">
      <strong>值班负责人升级确认</strong>
      <p class="muted">批准后将通知拖车并再次告知车主。此操作仅值班负责人可执行。</p>
      <label class="check"><input type="checkbox" id="esc-confirm">
        我是值班负责人 <b>${esc(u.display_name)}</b>,确认对车辆 ${esc(d.plate)} 升级拖移</label>
      <label>备注<input id="esc-note" placeholder="如:超时 1 小时,车主拒不挪车"></label>
      <button class="btn danger small" id="esc-ok">确认升级拖移</button></div>`;
    slot.querySelector("#esc-ok").addEventListener("click", () => {
      if (!slot.querySelector("#esc-confirm").checked) {
        toast("请勾选值班负责人确认项", "error"); return;
      }
      done(post("escalate", {
        confirm: true, note: slot.querySelector("#esc-note").value,
      }), "已批准升级拖移");
    });
  });

  root.querySelector('[data-act="tow-cancel"]')?.addEventListener("click", () => {
    const reason = prompt("取消拖移原因(将留痕):", "车主到场,承诺立即驶离");
    if (reason) done(post("tow-cancel", { reason }), "拖移已取消,事件回到巡查中");
  });

  root.querySelector('[data-act="depart"]')?.addEventListener("click", () => {
    const slot = root.querySelector("#action-slot");
    const canTow = d.status === "escalated";
    const emergency = !!d.is_emergency;
    slot.innerHTML = `<label>驶离方式
      <select id="dep-reason">
        <option value="self_drive">车主自行驶离</option>
        ${canTow ? `<option value="towed">依法拖离(已批准升级)</option>` : ""}
        ${emergency ? `<option value="emergency_release">紧急救援结束放行</option>` : ""}
      </select></label>
      <label>备注<input id="dep-note" placeholder="如:车主接通知后返回"></label>
      ${photoInput("dep-photo")}
      <button class="btn primary small" id="dep-ok">登记驶离</button>
      <p class="muted">登记后须由<strong>另一名保安</strong>复核通道恢复才能结案。</p>`;
    bindPhotoPreview(slot, "#dep-photo");
    slot.querySelector("#dep-ok").addEventListener("click", async () => {
      const photo = await readPhoto(slot.querySelector("#dep-photo"));
      done(post("depart", {
        reason: slot.querySelector("#dep-reason").value,
        note: slot.querySelector("#dep-note").value,
        photo: photo ? photo.split(",")[1] : null,
      }), "驶离已登记,等待另一保安复核");
    });
  });

  root.querySelector('[data-act="review"]')?.addEventListener("click", () => {
    const slot = root.querySelector("#action-slot");
    slot.innerHTML = `<label>复核备注
      <textarea id="rev-note">通道已恢复,无障碍物,消防车可正常通行</textarea></label>
      ${photoInput("rev-photo")}
      <button class="btn primary small" id="rev-ok">确认通道恢复,结案</button>
      <p class="muted">复核人:${esc(u.display_name)}(与驶离登记人 ${esc(d.departed_by_name)} 分离)</p>`;
    bindPhotoPreview(slot, "#rev-photo");
    slot.querySelector("#rev-ok").addEventListener("click", async () => {
      const photo = await readPhoto(slot.querySelector("#rev-photo"));
      done(post("review", {
        note: slot.querySelector("#rev-note").value,
        photo: photo ? photo.split(",")[1] : null,
      }), "复核通过,事件已结案");
    });
  });
}

function bindPhotoPreview(slot, sel) {
  const input = slot.querySelector(sel);
  input.addEventListener("change", () => {
    const f = input.files[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = () => {
      const pv = slot.querySelector(".photo-preview");
      pv.querySelector("img").src = rd.result;
      pv.classList.remove("hidden");
    };
    rd.readAsDataURL(f);
  });
}

// ---------- 统计(SVG,单蓝色系;状态色仅作状态点) ----------
async function renderStats() {
  const days = $("#stats-days").value;
  let s;
  try { s = await api(`/api/stats?days=${days}`); }
  catch (err) { toast(err.message, "error"); return; }

  const closed = s.by_status.closed || 0;
  const active = s.total - closed - (s.by_status.departed || 0);
  $("#stats-tiles").innerHTML = `
    ${tile(s.total, "占道事件总数", `最近 ${s.days} 天`)}
    ${tile(active, "处置中/待处理", "红色通道实时阻断")}
    ${tile(s.by_status.departed || 0, "待另一保安复核", "")}
    ${tile(closed, "已结案", "")}
    ${tile(s.avg_duration_min ?? "—", "平均占用时长",
      s.median_duration_min != null ? `中位数 ${s.median_duration_min} 分钟` : "",
      s.avg_duration_min != null ? " 分钟" : "")}
    ${tile(`${Math.round((s.contact_fail_rate ?? 0) * 100)}%`, "联系失败率", `${s.contact_fail}/${s.contact_total} 次联系失败留痕`)}
    ${tile(s.duplicate_merged, "重复上报合并", "未重复开单")}
    ${tile(s.plate_corrected, "车牌识别更正", "")}
  `;

  // 每日柱状(最近 days 天,补零)
  const counts = new Array(s.days).fill(0);
  s.daily.forEach(d => { if (d.day_offset >= 0 && d.day_offset < s.days) counts[d.day_offset] = d.c; });
  $("#chart-daily").innerHTML = barChart(counts, s.days, (i) => {
    const d = new Date(s.cutoff + i * 86400000);
    return `${d.getMonth() + 1}/${d.getDate()}`;
  });

  // 按楼栋
  const bdata = s.by_building;
  $("#chart-building").innerHTML = hBarChart(bdata.map(x => [x.building, x.c]));

  // 处置结果构成(互斥口径;拖移取消/识别更正等留痕指标在下方列表展示)
  const outcomes = [
    ["已结案·自行离场", Math.max(0, closed - s.towed)],
    ["已结案·依法拖离", s.towed],
    ["处置中", active],
    ["待另一保安复核", s.by_status.departed || 0],
  ];
  $("#chart-outcomes").innerHTML = hBarChart(outcomes, { neutral: true });

  // 关键指标
  $("#chart-misc").innerHTML = `<ul class="viz-dlist">
    <li><span>拖移取消后自行离场</span><b>${s.tow_cancelled} 件</b></li>
    <li><span>车牌识别错误更正</span><b>${s.plate_corrected} 次</b></li>
    <li><span>重复上报自动合并</span><b>${s.duplicate_merged} 次</b></li>
    <li><span>联系记录总数</span><b>${s.contact_total} 条</b></li>
    <li><span>其中失败/无人接/错号</span><b>${s.contact_fail} 条</b></li>
    <li><span>平均结案占用时长</span><b>${s.avg_duration_min ?? "—"} 分钟</b></li>
  </ul>`;
}
function tile(v, l, sub = "", suffix = "") {
  return `<div class="tile"><div class="v">${v}${suffix}</div><div class="l">${l}</div>
    ${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
}

function barChart(values, days, labeler) {
  const W = 520, H = 220, pad = { l: 30, r: 8, t: 14, b: 26 };
  const max = Math.max(1, ...values);
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const bw = Math.max(2, iw / values.length - 2);
  const ticks = [0, Math.ceil(max / 2), max];
  let grid = "", xlabels = "";
  ticks.forEach(t => {
    const y = pad.t + ih - (t / max) * ih;
    grid += `<line class="viz-grid" x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}"/>
      <text class="viz-label" x="${pad.l - 6}" y="${y + 3}" text-anchor="end">${t}</text>`;
  });
  const step = Math.max(1, Math.ceil(days / 8));
  const bars = values.map((v, i) => {
    const h = (v / max) * ih;
    const x = pad.l + i * (iw / values.length) + 1;
    const y = pad.t + ih - h;
    if (i % step === 0) {
      xlabels += `<text class="viz-label" x="${x + bw / 2}" y="${H - 8}" text-anchor="middle">${labeler(i)}</text>`;
    }
    return `<rect class="viz-bar" x="${x}" y="${y}" width="${bw}" height="${Math.max(0, h)}">
      <title>${labeler(i)}:${v} 件</title></rect>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="每日占道事件柱状图">
    ${grid}<line class="viz-axis" x1="${pad.l}" y1="${pad.t + ih}" x2="${W - pad.r}" y2="${pad.t + ih}"/>
    ${bars}${xlabels}</svg>`;
}

function hBarChart(pairs, opts = {}) {
  const max = Math.max(1, ...pairs.map(([, v]) => v));
  return pairs.map(([label, v], i) => {
    const pct = (v / max) * 100;
    return `<div style="margin:9px 0">
      <div style="display:flex;justify-content:space-between;font-size:12.5px;color:var(--ink-2)">
        <span>${esc(label)}</span><b style="color:var(--ink)">${v} 件</b></div>
      <div style="height:14px;background:var(--surface-2);border-radius:7px;margin-top:3px;overflow:hidden">
        <div class="${opts.neutral && i > 1 ? "viz-bar neutral" : "viz-bar"}"
          style="height:100%;width:${pct}%;border-radius:7px;min-width:${v ? 3 : 0}px"></div>
      </div></div>`;
  }).join("");
}

$("#stats-days").addEventListener("change", renderStats);

// ---------- 模拟控制 ----------
async function loadSimConfig() {
  try {
    const cfg = await api("/api/sim/config");
    $$('input[name=lpr]').forEach(r => r.checked = r.value === cfg.lpr_mode);
    $$('input[name=sms]').forEach(r => r.checked = r.value === cfg.sms_mode);
    renderFailTags(cfg.sms_fail_phones || []);
    $$('input[name=lpr],input[name=sms]').forEach(r => r.addEventListener("change", saveSimConfig));
  } catch (err) { toast(err.message, "error"); }
}
async function saveSimConfig() {
  const body = {
    lpr_mode: $$('input[name=lpr]').find(r => r.checked)?.value || "normal",
    sms_mode: $$('input[name=sms]').find(r => r.checked)?.value || "normal",
    sms_fail_phones: state.failPhones || [],
  };
  try { await api("/api/sim/config", { method: "POST", body: JSON.stringify(body) }); toast("模拟配置已保存"); }
  catch (err) { toast(err.message, "error"); }
}
function renderFailTags(phones) {
  state.failPhones = phones;
  $("#sim-fail-tags").innerHTML = phones.map(p =>
    `<span class="tag-pill">${esc(p)} <button data-rm="${esc(p)}">×</button></span>`).join("");
  $$("#sim-fail-tags [data-rm]").forEach(b => b.addEventListener("click", () => {
    state.failPhones = state.failPhones.filter(p => p !== b.dataset.rm);
    saveSimConfig().then(loadSimConfig);
  }));
}
$("#sim-fail-phone").addEventListener("keydown", async (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const p = e.target.value.trim();
  if (!/^\d{7,15}$/.test(p)) { toast("请输入 7-15 位手机号", "error"); return; }
  e.target.value = "";
  await saveSimConfig();
  const cfg = await api("/api/sim/config");
  renderFailTags([...new Set([...(cfg.sms_fail_phones || []), p])]);
  await saveSimConfig();
});

// ---------- 通知已读接口(路由注册在服务端仅 list/mark_read) ----------
// (mark_read 通过 POST /api/notifications/<id>/read,需在服务端注册)

initFilters();
boot();
})();
