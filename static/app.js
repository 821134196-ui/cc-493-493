/* 消防通道临停管控 —— 前端逻辑（原生 JS，无依赖） */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const state = {
    me: null,
    channels: [],
    events: [],
    currentTab: "map",
    detailId: null,
    pollTimer: null,
    reportPhoto: "",
    actionPhotos: {},   // detail 表单临时照片
  };

  const STATUS_TEXT = {
    open: "待接单", accepted: "已接单·到场核查", contacted: "处置中（已联系）",
    tow_requested: "拖移申请·待负责人审批", tow_approved: "已批准拖移",
    pending_review: "待清障复核", emergency: "紧急救援占道", closed: "已结案",
  };
  const STATUS_CLASS = {
    open: "processing", accepted: "processing", contacted: "processing",
    tow_requested: "pending", tow_approved: "pending",
    pending_review: "pending", emergency: "blocking", closed: "closed",
  };
  const TL_TEXT = {
    created: "创建事件单", repeat_report: "重复上报 · 已合并（未生成新单）",
    report_note: "补充上报备注", reentry: "车辆二次驶入 · 事件重开",
    accept: "保安接单巡查", contact: "登记联系记录", plate_corrected: "车牌识别错误 · 已更正",
    emergency: "标记为紧急救援占道", tow_requested: "申请升级拖移",
    tow_approved: "值班负责人确认升级拖移", tow_rejected: "值班负责人驳回拖移申请",
    tow_cancelled: "拖移取消", towed: "车辆已拖离", left: "登记车辆驶离",
    review_passed: "清障复核通过 · 事件结案", review_failed: "清障复核未通过 · 事件重开",
  };
  const TL_CLASS = {
    created: "tl-neutral", repeat_report: "tl-neutral", report_note: "tl-neutral",
    accept: "tl-neutral", contact: "tl-neutral", plate_corrected: "tl-warn",
    reentry: "tl-warn", emergency: "tl-danger", tow_requested: "tl-warn",
    tow_approved: "tl-danger", tow_rejected: "tl-neutral", tow_cancelled: "tl-neutral",
    towed: "tl-warn", left: "tl-neutral", review_passed: "tl-ok", review_failed: "tl-danger",
  };
  const BLOCKING = new Set(["open", "accepted", "contacted", "tow_requested",
    "tow_approved", "emergency"]);

  // ---------------- HTTP ----------------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin",
    });
    let data = null;
    try { data = await res.json(); } catch { /* ignore */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || `请求失败（${res.status}）`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  let toastTimer = null;
  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast" + (isError ? " error" : "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
  }

  function confirmModal(title, text) {
    return new Promise((resolve) => {
      $("#confirm-title").textContent = title;
      $("#confirm-text").textContent = text;
      const mask = $("#confirm-mask");
      mask.hidden = false;
      const done = (ok) => {
        mask.hidden = true;
        $("#confirm-ok").onclick = null;
        $("#confirm-cancel").onclick = null;
        resolve(ok);
      };
      $("#confirm-ok").onclick = () => done(true);
      $("#confirm-cancel").onclick = () => done(false);
    });
  }

  // ---------------- 登录 ----------------
  async function refreshMe() {
    try {
      const meta = await api("/api/me");
      state.me = meta.user;
      state.channels = meta.channels;
      state.sim = meta.sim;
      enterApp();
      return true;
    } catch (e) {
      if (e.status === 401) { showLogin(); return false; }
      throw e;
    }
  }

  function showLogin() {
    state.me = null;
    $("#login-view").hidden = false;
    $("#app").hidden = true;
  }

  function enterApp() {
    $("#login-view").hidden = true;
    $("#app").hidden = false;
    const roleText = { resident: "住户", guard: "保安", supervisor: "值班负责人" }[state.me.role];
    $("#user-badge").textContent =
      `${state.me.display_name}｜${roleText}${state.me.building_id ? `·${state.me.building_id}号楼` : ""}`;
    $$(".staff-only").forEach(el => el.hidden = state.me.role === "resident");
    switchTab(state.me.role === "resident" ? "events" : state.currentTab || "map");
    if (state.me.role === "resident") {
      $("#events-title").textContent = "与我车辆有关的通知";
    } else {
      $("#events-title").textContent = "事件";
    }
    renderReportChannels();
    renderSimStatic();
    refreshAll();
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => { refreshAll(true); }, 15000);
  }

  async function refreshAll(silent = false) {
    if (!state.me) return;
    try {
      const [evData] = await Promise.all([
        api("/api/events"),
        state.me.role !== "resident" ? api("/api/me").then(m => { state.channels = m.channels; }) : null,
      ]);
      state.events = evData.events;
      render();
      if (state.currentTab === "stats" && state.me.role !== "resident") renderStats(await api("/api/stats"));
      if (state.currentTab === "sim" && state.me.role !== "resident") renderOutbox((await api("/api/sms-outbox")).messages);
      if (state.detailId) renderDetail(await api(`/api/events/${state.detailId}`));
    } catch (e) {
      if (e.status === 401) { showLogin(); return; }
      if (!silent) toast(e.message, true);
    }
  }

  // ---------------- Tab ----------------
  function switchTab(tab) {
    if (tab === "stats" || tab === "sim" || tab === "review") {
      if (state.me && state.me.role === "resident") tab = "events";
    }
    state.currentTab = tab;
    $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
    $$(".view").forEach(v => v.hidden = v.id !== `view-${tab}`);
    if (tab === "map") renderMap();
    if (tab === "events") renderEventList();
    if (tab === "review") renderReviewList();
    if (tab === "stats" && state.me.role !== "resident")
      api("/api/stats").then(renderStats).catch(e => toast(e.message, true));
    if (tab === "sim" && state.me.role !== "resident") {
      renderSimStatic();
      api("/api/sms-outbox").then(d => renderOutbox(d.messages)).catch(() => {});
    }
  }

  // ---------------- 平面图 ----------------
  const BUILDINGS = [
    { x: 300, y: 185, w: 240, h: 225, label: "1号楼" },
    { x: 120, y: 200, w: 150, h: 112, label: "3号楼" },
    { x: 730, y: 300, w: 190, h: 200, label: "2号楼" },
  ];

  function polyPoints(str) { return str.split(";").map(p => p.split(",").map(Number)); }
  function centroid(str) {
    const pts = polyPoints(str);
    const n = pts.length;
    let area = 0, cx = 0, cy = 0;
    for (let i = 0; i < n; i++) {
      const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % n];
      const cross = x1 * y2 - x2 * y1;
      area += cross; cx += (x1 + x2) * cross; cy += (y1 + y2) * cross;
    }
    area /= 2;
    if (Math.abs(area) < 1) {
      return { x: pts.reduce((s, p) => s + p[0], 0) / n, y: pts.reduce((s, p) => s + p[1], 0) / n };
    }
    return { x: cx / (6 * area), y: cy / (6 * area) };
  }
  const svgEl = (tag, attrs = {}, text) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (text != null) el.textContent = text;
    return el;
  };

  function channelState(ch) {
    const evs = ch.active_events || [];
    // blocked 由服务端按通道上是否有阻断态事件计算（住户视图同样可用）
    const blocking = !!ch.blocked;
    const pending = evs.some(e => e.status === "pending_review");
    return { blocking, pending, evs };
  }

  function renderMap() {
    const svg = $("#map-svg");
    svg.innerHTML = "";
    svg.appendChild(svgEl("rect", { x: 0, y: 0, width: 1000, height: 640, class: "map-ground", rx: 14 }));

    // 装饰道路网
    svg.appendChild(svgEl("path", {
      d: "M40 560 H640 M640 560 V280 M40 100 H620",
      stroke: "#e1e6eb", "stroke-width": 26, fill: "none", "stroke-linecap": "round",
    }));

    // 楼栋
    for (const b of BUILDINGS) {
      svg.appendChild(svgEl("rect", { x: b.x, y: b.y, width: b.w, height: b.h, rx: 12, class: "map-building" }));
      svg.appendChild(svgEl("text", { x: b.x + b.w / 2, y: b.y + b.h / 2, class: "map-building-label" }, b.label));
    }

    // 通道（消防出入口为视觉锚点，红色只给正在阻断的通道）
    state.channels.forEach((ch, idx) => {
      const st = channelState(ch);
      const cls = "map-road clickable " + (st.blocking ? "blocking" : st.pending ? "has-active" : "");
      const road = svgEl("polygon", { points: ch.polygon.replace(/;/g, " "), class: cls, tabindex: 0 });
      road.appendChild(svgEl("title", {}, `${ch.name}（${ch.building}）${st.blocking ? " — 正在阻断！" : ""}`));
      road.addEventListener("click", () => onChannelClick(ch));
      svg.appendChild(road);

      svg.appendChild(svgEl("text", {
        x: ch.label_x, y: ch.label_y,
        class: "map-road-label " + (st.blocking ? "blocking" : ""),
      }, ch.name.replace(/楼?消防通道/, "")));

      // 消防出入口锚点：绿色“出”标识 + 名称
      svg.appendChild(svgEl("circle", { cx: ch.exit_x, cy: ch.exit_y, r: 16, class: "map-exit-halo" }));
      svg.appendChild(svgEl("rect", {
        x: ch.exit_x - 11, y: ch.exit_y - 11, width: 22, height: 22, rx: 5, class: "map-exit",
      }));
      svg.appendChild(svgEl("text", {
        x: ch.exit_x, y: ch.exit_y + 4.5, "font-size": 12, "font-weight": 700,
        fill: "#fff", "text-anchor": "middle",
      }, "出"));
      const lx = ch.exit_x + (ch.exit_x < 150 ? -4 : ch.exit_x > 900 ? 4 : 0);
      const anchor = svgEl("text", {
        x: ch.exit_x, y: ch.exit_y - 18, class: "map-exit-label", "text-anchor": "middle",
      }, ch.anchor);
      svg.appendChild(anchor);

      // 事件图钉（住户只显示涉及本人车辆的；通道红色只表示“正在阻断”，不暴露他人信息）
      const c = centroid(ch.polygon);
      const pins = state.me.role === "resident" ? st.evs.filter(e => e.mine) : st.evs;
      pins.forEach((ev, i) => {
        const px = c.x + (i - (pins.length - 1) / 2) * 26;
        // 住户视角不暴露事件状态，按通道是否正在阻断着色
        const isBlocking = ev.status ? BLOCKING.has(ev.status) : st.blocking;
        const g = svgEl("g", { style: "cursor:pointer" });
        g.appendChild(svgEl("circle", {
          cx: px, cy: c.y, r: 12,
          class: isBlocking ? "map-blocked-pin" : "map-review-pin",
        }));
        g.appendChild(svgEl("text", { x: px, y: c.y + 5, class: "map-pin-label" },
          ev.kind === "emergency" ? "急" : isBlocking ? "阻" : "核"));
        g.addEventListener("click", (e) => { e.stopPropagation(); openDetail(ev.id); });
        svg.appendChild(g);
      });
    });

    renderMapActiveList();
  }

  function onChannelClick(ch) {
    if (state.me.role === "resident") {
      const mine = ch.active_events.find(e => e.mine);
      if (mine) openDetail(mine.id);
      else toast("该通道当前畅通，或无涉及您车辆的通知");
      return;
    }
    if (!ch.in_scope) { toast("该通道不在你的楼栋负责范围"); return; }
    openReportSheet(ch.id);
  }

  function renderMapActiveList() {
    const wrap = $("#map-active-list");
    wrap.innerHTML = "";
    const active = state.events.filter(e => e.status !== "closed");
    if (!active.length) {
      wrap.innerHTML = `<div class="empty">✅ 所有可见消防通道当前畅通，无在案事件</div>`;
      return;
    }
    active.forEach(ev => wrap.appendChild(evCard(ev)));
  }

  // ---------------- 事件卡片 ----------------
  function fmtDuration(ev) {
    // 已结案用服务端口径；驶离/拖离后（待复核及以后）占用时长冻结在驶离时刻
    if (ev.status === "closed" || ev.leave_at) {
      if (ev.duration) return ev.duration;
    }
    // 前端按 found_at 实时计算
    const t0 = new Date(ev.found_at.replace(/-/g, "/")).getTime();
    const endAt = (ev.status === "closed" && ev.closed_at) ? ev.closed_at : ev.leave_at;
    const end = endAt ? new Date(endAt.replace(/-/g, "/")).getTime() : Date.now();
    let secs = Math.max(0, Math.floor((end - t0) / 1000));
    if (secs < 60) return `${secs}秒`;
    const m = Math.floor(secs / 60);
    if (m < 60) return `${m}分`;
    const h = Math.floor(m / 60), mm = m % 60;
    if (h < 48) return `${h}小时${mm}分`;
    return `${Math.floor(h / 24)}天${h % 24}小时`;
  }

  function evCard(ev) {
    const card = document.createElement("div");
    const isBlocking = BLOCKING.has(ev.status);
    const cls = ev.status === "closed" ? "closed" : ev.status === "pending_review" ? "pending"
      : isBlocking ? "blocking" : "pending";
    card.className = `ev-card ${cls}`;
    const mergedBadge = ev.merged_count > 0
      ? `<span class="badge merged">已合并重复上报 ×${ev.merged_count}</span>` : "";
    card.innerHTML = `
      <div class="ev-top">
        <span class="ev-plate">${ev.plate}${ev.kind === "emergency" ? ' <span class="badge blocking">紧急救援</span>' : ""}</span>
        <span class="badge ${STATUS_CLASS[ev.status]}">${STATUS_TEXT[ev.status]}</span>
      </div>
      <div class="ev-meta">
        <span>占用区域</span><b>${ev.channel}（${ev.building}）</b>
        <span>发现时间</span><b>${ev.found_at}</b>
        <span class="ev-duration">占用时长</span><b class="ev-duration">${fmtDuration(ev)}</b>
        <span>发现人</span><b>${ev.found_by}</b>
        <span>实际处置人</span><b>${ev.handled_by || "—（尚未处置）"}</b>
        ${ev.reviewer ? `<span>清障复核人</span><b>${ev.reviewer}</b>` : ""}
      </div>
      ${mergedBadge ? `<div>${mergedBadge}</div>` : ""}
      <div class="ev-actions"></div>`;
    const actions = card.querySelector(".ev-actions");
    actions.appendChild(btn("查看详情", () => openDetail(ev.id), "btn-ghost btn-sm"));
    if (state.me.role !== "resident") appendQuickActions(actions, ev);
    card.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      openDetail(ev.id);
    });
    return card;
  }

  function btn(text, onClick, extra = "") {
    const b = document.createElement("button");
    b.className = `btn ${extra}`;
    b.textContent = text;
    b.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
    return b;
  }

  function appendQuickActions(actions, ev) {
    const role = state.me.role;
    const add = (text, fn, cls = "btn-primary btn-sm") => actions.appendChild(btn(text, fn, cls));
    if (ev.status === "open") add("接单", () => act(ev.id, "accept", {}));
    if (ev.status === "pending_review" && ev.handled_by !== state.me.display_name)
      add("去复核", () => { switchTab("review"); }, "btn-outline btn-sm");
    if (ev.status === "tow_requested" && role === "supervisor")
      add("审批拖移", () => openDetail(ev.id), "btn-danger btn-sm");
  }

  function renderEventList() {
    const wrap = $("#event-list");
    const kw = ($("#event-search").value || "").trim().toUpperCase();
    const list = state.events.filter(ev => {
      if (!kw) return true;
      return ev.plate.toUpperCase().includes(kw) || ev.channel.includes(kw) || ev.building.includes(kw);
    });
    wrap.innerHTML = "";
    if (!list.length) {
      wrap.innerHTML = `<div class="empty">${state.me.role === "resident" ? "暂无涉及您车辆的通知" : "暂无事件"}</div>`;
      return;
    }
    list.forEach(ev => wrap.appendChild(evCard(ev)));
    updateTabs();
  }

  function renderReviewList() {
    const wrap = $("#review-list");
    wrap.innerHTML = "";
    const pendings = state.events.filter(e => e.status === "pending_review");
    if (!pendings.length) {
      wrap.innerHTML = `<div class="empty">暂无待复核事件。车辆驶离/拖离后，须由另一名保安现场核对通道恢复。</div>`;
      return;
    }
    pendings.forEach(ev => {
      const card = document.createElement("div");
      card.className = "ev-card pending";
      const samePerson = ev.handled_by === state.me.display_name;
      card.innerHTML = `
        <div class="ev-top">
          <span class="ev-plate">${ev.plate}</span>
          <span class="badge pending">待清障复核</span>
        </div>
        <div class="ev-meta">
          <span>占用区域</span><b>${ev.channel}</b>
          <span>实际处置人</span><b>${ev.handled_by}</b>
          <span>驶离/拖离时间</span><b>${ev.leave_at || "—"}</b>
          <span>处置结果</span><b>${ev.resolution === "towed" ? "已拖移" : ev.resolution === "emergency" ? "紧急救援解除" : "车主自行驶离"}</b>
        </div>
        ${samePerson ? `<p class="guard-note">⚠ 你是该事件的实际处置人，按规定不能自行复核，请通知另一名保安（如同班同事）操作。</p>` : ""}
        <div class="ev-actions"></div>`;
      const acts = card.querySelector(".ev-actions");
      acts.appendChild(btn("查看并复核", () => openDetail(ev.id), "btn-primary btn-sm"));
      wrap.appendChild(card);
    });
  }

  function updateTabs() {
    const blocking = state.events.some(e => BLOCKING.has(e.status));
    $("#tab-events-dot").hidden = !blocking;
    const reviewable = state.events.some(
      e => e.status === "pending_review" && e.handled_by !== state.me.display_name);
    $("#tab-review-dot").hidden = !reviewable;
  }

  function render() {
    updateTabs();
    if (state.currentTab === "map") renderMap();
    if (state.currentTab === "events") renderEventList();
    if (state.currentTab === "review") renderReviewList();
  }

  // ---------------- 事件详情 ----------------
  async function openDetail(id) {
    $("#detail-sheet").hidden = false;
    state.detailId = id;
    try {
      renderDetail(await api(`/api/events/${id}`));
    } catch (e) { toast(e.message, true); }
  }

  function closeSheets() {
    $("#detail-sheet").hidden = true;
    $("#report-sheet").hidden = true;
    state.detailId = null;
    state.reportPhoto = "";
  }

  function renderDetail(d) {
    state.actionPhotos = {};
    const isBlocking = BLOCKING.has(d.status);
    $("#detail-title").textContent = `#${d.id} ${d.plate} · ${d.channel}`;
    const body = $("#detail-body");
    body.innerHTML = `
      <div class="detail-summary">
        <span>状态</span><b><span class="badge ${STATUS_CLASS[d.status]}">${STATUS_TEXT[d.status]}</span>
          ${d.kind === "emergency" ? '<span class="badge blocking">紧急救援占道</span>' : ""}
          ${d.merged_count > 0 ? `<span class="badge merged">合并重复上报 ×${d.merged_count}</span>` : ""}</b>
        <span>占用区域</span><b>${d.channel}（${d.building}）</b>
        <span>发现时间</span><b>${d.found_at}</b>
        <span>${d.closed_at ? "总占用时长" : "已占用"}</span><b style="color:${isBlocking ? "var(--danger)" : ""}">${fmtDuration(d)}</b>
        <span>发现人</span><b>${d.found_by}</b>
        <span>接单保安</span><b>${d.assigned_guard || "—"}</b>
        <span>实际处置人</span><b>${d.handled_by || "—"}</b>
        <span>清障复核人</span><b>${d.reviewer || "—"}</b>
        ${d.closed_at ? `<span>结案时间</span><b>${d.closed_at}</b>` : ""}
      </div>

      <div class="detail-section">
        <h4>📷 现场照片</h4>
        ${d.photos.length ? `<div class="photo-strip">${d.photos.map(p => `
          <figure style="margin:0">
            <a href="${p.url}" target="_blank"><img src="${p.url}" alt="${p.caption}"></a>
            <figcaption class="photo-caption">${p.ts.slice(5)} ${p.by}<br>${p.kind === "clearance" ? "🧹 清障复核照" : p.caption}</figcaption>
          </figure>`).join("")}</div>`
        : `<p class="form-hint">暂无照片</p>`}
      </div>

      <div class="detail-section">
        <h4>📞 联系记录</h4>
        <div id="contact-list">${renderContacts(d.contacts)}</div>
      </div>

      <div class="detail-section">
        <h4>🕒 事件时间线</h4>
        <ul class="timeline">
          ${d.timeline.map(t => `
            <li class="${TL_CLASS[t.type] || "tl-neutral"}">
              <div class="tl-ts">${t.ts} · ${t.actor}</div>
              <div class="tl-text">${TL_TEXT[t.type] || t.type}${timelineDetail(t)}</div>
            </li>`).join("")}
        </ul>
      </div>

      <div class="detail-section" id="detail-actions-wrap"></div>
    `;
    if (d.can_handle) renderDetailActions(d);
    else body.insertAdjacentHTML("beforeend",
      `<p class="section-hint">🔒 您以住户身份查看此通知。事件处置流程对住户只读，封闭事件不可改写。</p>`);
  }

  function timelineDetail(t) {
    const x = t.detail;
    if (!x) return "";
    const bits = [];
    if (x.note) bits.push(x.note);
    if (x.reason) bits.push(`原因：${x.reason}`);
    if (x.old_plate) bits.push(`车牌更正：${x.old_plate} → ${x.new_plate}`);
    if (x.method) bits.push(`${({ sms: "短信", phone: "电话", onsite: "现场" })[x.method]}：${contactResultText(x.method, x.result)}${x.gateway === "failed" ? "（模拟网关：发送失败）" : ""}`);
    if (x.handler) bits.push(`处置人：${x.handler}`);
    if (x.reviewer) bits.push(`复核人：${x.reviewer}`);
    if (x.awaiting) bits.push(x.awaiting);
    return bits.length ? `<div class="tl-detail">${bits.join("；")}</div>` : "";
  }

  function contactResultText(method, result) {
    return {
      delivered: "已送达/接通", failed: "发送失败", no_answer: "无人接听",
      promised_left: "承诺立即驶离", refused: "拒绝驶离",
    }[result] || result;
  }

  function renderContacts(list) {
    if (!list.length) return `<p class="form-hint">暂无联系记录</p>`;
    return list.map(c => `
      <div class="contact-row">
        <div class="cr-head"><b>${({ sms: "短信", phone: "电话", onsite: "现场告知" })[c.method]}</b>
          <span class="${c.result === "failed" ? "result-failed" : "result-ok"}">${contactResultText(c.method, c.result)}</span></div>
        <span class="tl-ts">${c.ts} · ${c.user_name} · ${c.phone}</span>
        ${c.content ? `<span class="form-hint">${c.content}</span>` : ""}
      </div>`).join("");
  }

  function photoInputHtml(key) {
    return `<label class="photo-take-label" style="max-width:260px">
      <input type="file" accept="image/*" capture="environment" data-photo-key="${key}" hidden>
      <span class="photo-take-btn" style="padding:10px">
        <b>📷 拍照留存</b><small>点击调起相机/选图</small>
      </span>
    </label>
    <img class="photo-preview" data-preview="${key}" alt="照片预览" hidden style="max-width:220px">`;
  }

  function renderDetailActions(d) {
    const wrap = $("#detail-actions-wrap");
    wrap.innerHTML = `<h4>🛠 处置操作</h4><div class="detail-actions"></div>
      <div id="inline-forms" style="display:grid;gap:12px;margin-top:10px"></div>`;
    const acts = wrap.querySelector(".detail-actions");
    const forms = $("#inline-forms");
    const me = state.me;
    const add = (text, cls, fn) => acts.appendChild(btn(text, fn, cls));

    if (d.status === "open") add("📥 接单巡查", "btn-primary", () => act(d.id, "accept", {}));
    if (["accepted", "contacted"].includes(d.status) || d.tow_status === "rejected") {
      add("📞 登记联系", "btn-outline", () => toggleContactForm(d, forms));
      add("✏️ 更正车牌", "btn-ghost", () => toggleCorrectForm(d, forms));
      add("🚗 登记驶离", "btn-primary", () => toggleLeaveForm(d, forms));
    }
    if (["open", "accepted", "contacted"].includes(d.status) && d.kind !== "emergency")
      add("🚑 紧急救援占道", "btn-ghost", () => act(d.id, "emergency", { reason: "紧急救援车辆占道" }));
    if (d.kind === "emergency" && d.status === "emergency")
      add("🚗 救援结束·登记驶离", "btn-primary", () => toggleLeaveForm(d, forms));
    if (["accepted", "contacted"].includes(d.status))
      add("⬆ 申请拖移", "btn-danger", () => toggleTowReqForm(d, forms));
    if (d.status === "tow_requested") {
      if (me.role === "supervisor") {
        add("✅ 确认升级拖移", "btn-danger", async () => {
          const ok = await confirmModal("值班负责人确认",
            `确认对 ${d.plate} 升级拖移？此操作需要值班负责人二次确认，确认后将通知拖车队。`);
          if (ok) act(d.id, "tow-approve", {});
        });
        add("驳回申请", "btn-ghost", () => toggleTowRejectForm(d, forms));
      } else {
        acts.insertAdjacentHTML("beforeend",
          `<span class="badge pending">等待值班负责人审批…</span>`);
      }
      add("取消拖移", "btn-ghost", () => toggleTowCancelForm(d, forms));
    }
    if (d.status === "tow_approved") {
      add("已拖离·登记", "btn-primary", () => toggleTowCompleteForm(d, forms));
      add("取消拖移", "btn-ghost", () => toggleTowCancelForm(d, forms));
    }
    if (d.status === "pending_review") {
      if (d.i_am_handler) {
        acts.insertAdjacentHTML("beforeend",
          `<span class="guard-note">⚠ 你是实际处置人，须由另一名保安复核。可退出后切换 guard2 账号演示。</span>`);
      } else {
        add("🧹 清障复核", "btn-primary", () => toggleReviewForm(d, forms));
      }
    }
    if (d.status === "closed") {
      acts.insertAdjacentHTML("beforeend",
        `<span class="badge closed">事件已封闭，不可改写</span>`);
    }
  }

  function showForm(forms, html) {
    forms.innerHTML = `<div class="inline-form">${html}</div>`;
    $$("input[type=file][data-photo-key]", forms).forEach(input => {
      input.addEventListener("change", () => readPhoto(input));
    });
    return forms.querySelector(".inline-form");
  }

  function toggleContactForm(d, forms) {
    const f = showForm(forms, `
      <label>联系方式
        <select id="cf-method">
          <option value="sms">短信（走模拟网关）</option>
          <option value="phone">电话</option>
          <option value="onsite">现场告知</option>
        </select></label>
      <label>联系结果
        <select id="cf-result">
          <option value="delivered">已送达 / 已接通</option>
          <option value="failed">发送失败（短信）</option>
        </select></label>
      <div class="btn-row"><button class="btn btn-ghost" data-cancel>取消</button>
        <button class="btn btn-primary" id="cf-submit">提交联系记录</button></div>`);
    const methodSel = $("#cf-method", f), resultSel = $("#cf-result", f);
    const syncResults = () => {
      const opts = {
        sms: [["delivered", "已送达"], ["failed", "发送失败"]],
        phone: [["delivered", "已接通"], ["no_answer", "无人接听"],
          ["promised_left", "承诺立即驶离"], ["refused", "拒绝驶离"]],
        onsite: [["promised_left", "承诺立即驶离"], ["refused", "拒绝驶离"]],
      }[methodSel.value];
      resultSel.innerHTML = opts.map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
    };
    methodSel.addEventListener("change", syncResults);
    syncResults();
    f.querySelector("[data-cancel]").onclick = () => (forms.innerHTML = "");
    $("#cf-submit", f).onclick = () =>
      act(d.id, "contact", { method: methodSel.value, result: resultSel.value });
  }

  function toggleCorrectForm(d, forms) {
    const f = showForm(forms, `
      <label>现场核对后的正确车牌<input id="pf-plate" class="input" value="${d.plate}"></label>
      <div class="btn-row"><button class="btn btn-ghost" data-cancel>取消</button>
        <button class="btn btn-primary" id="pf-submit">更正并留痕</button></div>`);
    f.querySelector("[data-cancel]").onclick = () => (forms.innerHTML = "");
    $("#pf-submit", f).onclick = () =>
      act(d.id, "correct-plate", { new_plate: $("#pf-plate", f).value });
  }

  function toggleLeaveForm(d, forms) {
    const f = showForm(forms, `
      <label>驶离后现场照片（证明通道恢复，供复核）${photoInputHtml("leave")}</label>
      <div class="btn-row"><button class="btn btn-ghost" data-cancel>取消</button>
        <button class="btn btn-primary" id="lv-submit">登记驶离，送复核</button></div>`);
    bindCancelPhotoForm(f, forms);
    $("#lv-submit", f).onclick = () => act(d.id, "leave", { photo: state.actionPhotos.leave || "" });
  }

  function toggleTowReqForm(d, forms) {
    const f = showForm(forms, `
      <label>申请理由<textarea id="tr-reason" rows="2">多次联系无果，车辆持续阻断消防通道，申请升级拖移</textarea></label>
      <div class="btn-row"><button class="btn btn-ghost" data-cancel>取消</button>
        <button class="btn btn-danger" id="tr-submit">提交拖移申请</button></div>`);
    f.querySelector("[data-cancel]").onclick = () => (forms.innerHTML = "");
    $("#tr-submit", f).onclick = async () => {
      const ok = await confirmModal("提交拖移申请", "提交后将由值班负责人审批确认，确定提交？");
      if (ok) act(d.id, "tow-request", { reason: $("#tr-reason", f).value });
    };
  }

  function toggleTowRejectForm(d, forms) {
    const f = showForm(forms, `
      <label>驳回原因<input id="rj-reason" class="input" value="车主已答应挪车，暂不拖移"></label>
      <div class="btn-row"><button class="btn btn-ghost" data-cancel>取消</button>
        <button class="btn btn-primary" id="rj-submit">驳回</button></div>`);
    bindCancelPhotoForm(f, forms);
    $("#rj-submit", f).onclick = () => act(d.id, "tow-reject", { reason: $("#rj-reason", f).value });
  }

  function toggleTowCancelForm(d, forms) {
    const f = showForm(forms, `
      <label>取消原因<input id="tc-reason" class="input" value="车主已现场挪车，拖移取消"></label>
      <div class="btn-row"><button class="btn btn-ghost" data-cancel>取消</button>
        <button class="btn btn-primary" id="tc-submit">确认取消拖移</button></div>`);
    bindCancelPhotoForm(f, forms);
    $("#tc-submit", f).onclick = () => act(d.id, "tow-cancel", { reason: $("#tc-reason", f).value });
  }

  function toggleTowCompleteForm(d, forms) {
    const f = showForm(forms, `
      <label>拖离后现场照片${photoInputHtml("tow")}</label>
      <div class="btn-row"><button class="btn btn-ghost" data-cancel>取消</button>
        <button class="btn btn-primary" id="tw-submit">登记拖离，送复核</button></div>`);
    bindCancelPhotoForm(f, forms);
    $("#tw-submit", f).onclick = () => act(d.id, "tow-complete", { photo: state.actionPhotos.tow || "" });
  }

  function toggleReviewForm(d, forms) {
    const f = showForm(forms, `
      <p class="form-hint">复核人：<b>${state.me.display_name}</b>；实际处置人：<b>${d.handled_by}</b>（二者已确认不同）</p>
      <label>清障复核照片（通道恢复全貌）${photoInputHtml("review")}</label>
      <label>复核备注<input id="rv-note" class="input" placeholder="如：通道已恢复畅通，无遗留物"></label>
      <div class="btn-row">
        <button class="btn btn-danger" id="rv-fail">复核不通过（通道未恢复）</button>
        <button class="btn btn-primary" id="rv-pass">复核通过 · 结案</button>
      </div>`);
    bindCancelPhotoForm(f, forms);
    $("#rv-pass", f).onclick = () =>
      act(d.id, "review", { passed: true, note: $("#rv-note", f).value,
        photo: state.actionPhotos.review || "" });
    $("#rv-fail", f).onclick = () =>
      act(d.id, "review", { passed: false, note: $("#rv-note", f).value || "通道未恢复畅通",
        photo: state.actionPhotos.review || "" });
  }

  function bindCancelPhotoForm(f, forms) {
    f.querySelector("[data-cancel]")?.addEventListener("click", () => (forms.innerHTML = ""));
  }

  function readPhoto(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const key = input.dataset.photoKey;
      state.actionPhotos[key] = reader.result;
      const preview = document.querySelector(`img[data-preview="${key}"]`);
      if (preview) { preview.src = reader.result; preview.hidden = false; }
    };
    reader.readAsDataURL(file);
  }

  async function act(id, action, body) {
    try {
      const r = await api(`/api/events/${id}/${action}`, { method: "POST", body });
      toast(r.message || "操作成功");
      await refreshAll();
      if (state.detailId) openDetail(id);
    } catch (e) { toast(e.message, true); }
  }

  // ---------------- 上报表单 ----------------
  function renderReportChannels() {
    const sel = $("#report-channel");
    const scope = state.channels.filter(c =>
      state.me.role === "supervisor" || c.building_id === state.me.building_id);
    sel.innerHTML = scope.map(c =>
      `<option value="${c.id}">${c.name}（${c.building}）</option>`).join("");
  }

  function openReportSheet(channelId) {
    if (state.me.role === "resident") { toast("住户无上报权限，请由保安或值班负责人操作"); return; }
    $("#report-sheet").hidden = false;
    $("#report-hint").textContent = "";
    if (channelId) $("#report-channel").value = String(channelId);
  }

  function bindReport() {
    const fileInput = $("#report-photo-input");
    fileInput.addEventListener("change", () => {
      const file = fileInput.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        state.reportPhoto = reader.result;
        const img = $("#report-photo-preview");
        img.src = reader.result; img.hidden = false;
      };
      reader.readAsDataURL(file);
    });

    $("#lpr-btn").addEventListener("click", async () => {
      try {
        const r = await api("/api/lpr");
        $("#report-plate").value = r.plate;
        toast(`识别结果：${r.plate}（${r.source}），识别有误可直接手工更正`);
      } catch (e) { toast(e.message, true); }
    });

    $("#report-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        const r = await api("/api/events", {
          method: "POST",
          body: {
            channel_id: Number(fd.get("channel_id")),
            plate_display: fd.get("plate_display"),
            photo: state.reportPhoto || "",
            note: fd.get("note") || "",
            kind: fd.get("kind") ? "emergency" : "violation",
          },
        });
        toast(r.message);
        e.target.reset();
        state.reportPhoto = "";
        $("#report-photo-preview").hidden = true;
        $("#report-sheet").hidden = true;
        await refreshAll();
        openDetail(r.id);
      } catch (err) {
        $("#report-hint").textContent = err.message;
        toast(err.message, true);
      }
    });
  }

  // ---------------- 统计 ----------------
  function durText(secs) {
    if (secs < 60) return `${secs}秒`;
    const m = Math.floor(secs / 60);
    if (m < 60) return `${m}分`;
    const h = Math.floor(m / 60);
    return `${h}小时${m % 60}分`;
  }

  function renderStats(s) {
    const tiles = [
      ["事件总数", s.total, "blue"], ["在案未结", s.active, s.blocking ? "danger" : "gray"],
      ["正在阻断通道", s.blocking, "danger"], ["已结案", s.closed, "green"],
      ["平均结案耗时", s.avg_closure, "gray"], ["紧急救援占道", s.emergency, "gray"],
      ["已拖移", s.towed, "gray"], ["合并重复上报", s.merged_reports, "gray"],
    ];
    $("#stats-tiles").innerHTML = tiles.map(([l, v, c]) =>
      `<div class="stat-tile ${c}"><div class="num">${typeof v === "number" ? v : v}</div><div class="lbl">${l}</div></div>`).join("");

    // 各楼栋堆叠条：灰=已结案 蓝=在案非阻断 红=正在阻断（红色仅表阻断）
    const maxTotal = Math.max(1, ...s.by_building.map(b => b.total));
    $("#chart-building").innerHTML = s.by_building.map(b => {
      const nonBlocking = Math.max(0, b.active - b.blocking);
      const closed = b.total - b.active;
      const seg = (val, color) =>
        `<span class="bar-seg" style="width:${(val / maxTotal * 100).toFixed(1)}%;background:${color}"></span>`;
      return `<div class="bar-row">
        <span>${b.building.replace("梧桐苑", "")}</span>
        <span class="bar-track" style="display:flex;gap:2px">
          ${seg(closed, "#9aa3af")}${seg(nonBlocking, "#2a78d6")}${seg(b.blocking, "#d03b3b")}
        </span><span class="bar-val">${b.total}</span></div>`;
    }).join("") + `
      <p class="form-hint" style="margin-top:8px">
        <i class="lg" style="background:#9aa3af"></i> 已结案
        <i class="lg" style="background:#2a78d6"></i> 在案非阻断
        <i class="lg" style="background:#d03b3b"></i> 正在阻断</p>`;

    $("#table-building").innerHTML = `
      <thead><tr><th>楼栋</th><th>总数</th><th>已结案</th><th>在案</th><th>正在阻断</th></tr></thead>
      <tbody>${s.by_building.map(b => `<tr>
        <td>${b.building}</td><td>${b.total}</td><td>${b.total - b.active}</td>
        <td>${b.active}</td><td>${b.blocking}</td></tr>`).join("")}</tbody>`;

    $("#stats-extra").innerHTML = `<div class="kv-list">
      <div class="kv"><span>拖移申请总量（含批准/驳回/取消）</span><b>${s.tow_requested}</b></div>
      <div class="kv"><span>实际拖移车辆</span><b>${s.towed}</b></div>
      <div class="kv"><span>紧急救援占道</span><b>${s.emergency}</b></div>
      <div class="kv"><span>重复上报合并次数</span><b>${s.merged_reports}</b></div>
      <div class="kv"><span>已结案平均处置耗时</span><b>${durText(s.avg_closure_secs)}</b></div>
    </div>`;
  }

  // ---------------- 模拟控制台 ----------------
  function renderSimStatic() {
    if (state.me.role === "resident") return;
    const mode = state.sim ? state.sim.sms_mode : "normal";
    $("#sms-mode-label").textContent = mode === "fail" ? "失败（短信一律发送失败）" : "正常送达";
    $("#sms-mode-label").style.color = mode === "fail" ? "var(--danger)" : "var(--exit-green)";
  }

  function renderOutbox(list) {
    const wrap = $("#sms-outbox");
    if (!list.length) { wrap.innerHTML = `<p class="form-hint">暂无短信</p>`; return; }
    wrap.innerHTML = list.map(m => `
      <div class="sms-item">
        <span>${m.ts}<br><small>事件 #${m.event_id} · ${m.phone}</small></span>
        <span class="st-${m.status}">${m.status === "failed" ? "发送失败" : "已送达"}</span>
        <span>${m.content}</span>
      </div>`).join("");
  }

  function bindSim() {
    $("#sms-normal").onclick = async () => {
      await api("/api/sim/sms-mode", { method: "POST", body: { mode: "normal" } });
      state.sim.sms_mode = "normal"; renderSimStatic(); toast("短信网关：正常送达");
    };
    $("#sms-fail").onclick = async () => {
      await api("/api/sim/sms-mode", { method: "POST", body: { mode: "fail" } });
      state.sim.sms_mode = "fail"; renderSimStatic(); toast("短信网关：失败模式已开启");
    };
    $("#lpr-set").onclick = async () => {
      const plate = $("#lpr-input").value;
      await api("/api/sim/lpr", { method: "POST", body: { plate } });
      state.sim.lpr_next = plate; toast(plate ? `下一次识别将返回「${plate}」` : "已恢复随机模拟");
    };
    $("#lpr-clear").onclick = async () => {
      $("#lpr-input").value = "";
      await api("/api/sim/lpr", { method: "POST", body: { plate: "" } });
      state.sim.lpr_next = ""; toast("已清除注入");
    };
  }

  // ---------------- 全局绑定 ----------------
  function bind() {
    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        const r = await api("/api/login", {
          method: "POST",
          body: { username: fd.get("username"), password: fd.get("password") },
        });
        toast(r.message);
        await refreshMe();
      } catch (err) {
        $("#login-error").textContent = err.message;
        $("#login-error").hidden = false;
      }
    });
    $$(".demo-grid button").forEach(b => b.addEventListener("click", () => {
      $("#login-form [name=username]").value = b.dataset.u;
      $("#login-form [name=password]").value = "123456";
    }));
    $("#logout-btn").addEventListener("click", async () => {
      await api("/api/logout", { method: "POST" }).catch(() => {});
      showLogin();
    });
    $$(".tab").forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));
    $$("[data-close-sheet]").forEach(b => b.addEventListener("click", closeSheets));
    $("#detail-sheet").addEventListener("click", (e) => { if (e.target.id === "detail-sheet") closeSheets(); });
    $("#report-sheet").addEventListener("click", (e) => { if (e.target.id === "report-sheet") closeSheets(); });
    $("#report-open-btn").addEventListener("click", () => openReportSheet());
    $("#event-search").addEventListener("input", renderEventList);
    bindReport();
    bindSim();
  }

  bind();
  refreshMe();
  setInterval(() => {
    // 实时跳动的占用时长
    if (state.currentTab === "map") renderMapActiveList();
    if (state.currentTab === "events") renderEventList();
    if (state.currentTab === "review") renderReviewList();
  }, 30000);
})();
