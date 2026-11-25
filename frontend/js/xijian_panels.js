// 犀鉴 · 房间 / 信任 / 自治面板（原生 JS，无构建工具）
//
// 为什么单独一个文件而不是塞进 app.js：
// app.js 负责 3D 场景与对话，本文件只做三件"只读展示 + 两个显式动作"
// （撤销、接受/否决建议）。分开后，前端坏一块不会拖垮主界面。

const STATE_LABEL = {
  present_confident: "有人",
  empty_confident: "无人",
  uncertain: "不确定",
  degraded: "降级",
};

const $ = (sel) => document.querySelector(sel);

function badge(state) {
  const cls = {
    present_confident: "ok",
    empty_confident: "empty",
    uncertain: "warn",
    degraded: "bad",
  }[state] || "warn";
  return `<span class="xj-badge ${cls}">${STATE_LABEL[state] || state || "--"}</span>`;
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} ${r.status}`);
  return r.json();
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[c]));
}

// ---------------- 三个视图 ----------------
async function renderRooms() {
  const data = await getJSON("/api/rooms");
  const rooms = data.summary || {};
  const nodes = (data.nodes && data.nodes.nodes) || [];
  const ids = Object.keys(rooms);
  if (!ids.length) {
    return `<div class="xj-empty">暂无房间状态。CSI 未启用时这里为空；可用
      <code>POST /api/csi/replay</code> 回放节点事件。</div>`;
  }
  return ids.map((id) => {
    const r = rooms[id];
    return `<div class="xj-row">
      <div class="xj-row-head">
        <b>${esc(r.label || id)}</b>${badge(r.state)}
        <span class="xj-dim">置信度 ${(r.confidence ?? 0).toFixed(2)}</span>
      </div>
      <div class="xj-dim">${esc(r.reason || "")}</div>
      <div class="xj-dim">旁证：${(r.sources || []).join(" / ") || "—"}</div>
    </div>`;
  }).join("") + `<div class="xj-dim">节点在线 ${nodes.filter(n => n.online).length}/${nodes.length}</div>`;
}

async function renderTrust() {
  const data = await getJSON("/api/trust/recent?limit=8");
  const decisions = data.decisions || [];
  const undoable = data.undoable || [];
  const rows = decisions.map((d) => `<div class="xj-row">
      <div class="xj-row-head"><code>${esc(d.decision_id)}</code>
        <span class="xj-dim">${esc(d.source)}</span>
        ${d.failed_layer ? `<span class="xj-badge bad">${esc(d.failed_layer)}</span>` : ""}
      </div>
    </div>`).join("");
  const undoRows = undoable.map((u) => `<div class="xj-row">
      <div class="xj-row-head"><b>${esc(u.device_name)}</b>
        <span class="xj-dim">${esc(u.command)}</span>
      </div>
      <div class="xj-actions">
        <button class="xj-btn" data-undo="${esc(u.action_id)}">撤销</button>
        <span class="xj-dim">${(u.changed || []).join(",")}</span>
      </div>
    </div>`).join("");
  return `<div class="xj-sub">最近决策</div>${rows || '<div class="xj-empty">暂无</div>'}
    <div class="xj-sub">可撤销（10 分钟窗口）</div>${undoRows || '<div class="xj-empty">暂无可撤销动作</div>'}`;
}

async function renderAdapt() {
  const data = await getJSON("/api/adaptation");
  const st = data.state || {};
  const proposals = data.proposals || [];
  const rows = proposals.map((p) => `<div class="xj-row">
      <div class="xj-row-head"><b>${esc(p.device_name)}</b>
        <span class="xj-dim">${esc(p.command)} ${esc(JSON.stringify(p.params))}</span>
        ${p.gate_ok ? "" : `<span class="xj-badge bad">${esc(p.gate_reason || "门控未过")}</span>`}
      </div>
      <div class="xj-dim">${esc(p.reason || "")}</div>
      <div class="xj-actions">
        <button class="xj-btn" data-accept="${esc(p.proposal_id)}">接受</button>
        <button class="xj-btn ghost" data-reject="${esc(p.proposal_id)}">否决</button>
      </div>
    </div>`).join("");
  return `<div class="xj-row">
      <div class="xj-row-head"><b>等级</b><span class="xj-badge warn">${esc(st.level)}</span>
        <span class="xj-dim">模式 ${esc(st.mode)}</span></div>
      <div class="xj-dim">enabled=${data.enabled} · 样本门槛 ${data.cold_start_samples} ·
        反馈 ${st.feedback ?? 0} · 已执行 ${st.applied ?? 0}</div>
    </div>
    <div class="xj-sub">当前提议</div>${rows || '<div class="xj-empty">暂无提议（L5 默认关闭；偏好带样本不足时不出建议）</div>'}`;
}

// ---------------- 外壳 ----------------
const RENDERERS = { rooms: renderRooms, trust: renderTrust, adapt: renderAdapt };
let current = "rooms";

function injectStyle() {
  if (document.getElementById("xj-style")) return;
  const style = document.createElement("style");
  style.id = "xj-style";
  style.textContent = `
  #xijian-panel{position:fixed;right:12px;bottom:12px;width:340px;max-height:46vh;
    background:rgba(16,20,28,.92);color:#e8eef7;border:1px solid #2c3648;border-radius:10px;
    font:12px/1.5 system-ui,"Segoe UI",sans-serif;z-index:900;display:flex;flex-direction:column;
    box-shadow:0 8px 24px rgba(0,0,0,.35);backdrop-filter:blur(6px)}
  #xijian-panel .xj-tabs{display:flex;gap:4px;padding:8px;border-bottom:1px solid #2c3648}
  #xijian-panel .xj-tabs button{flex:1;background:transparent;color:#9fb0c8;border:1px solid #2c3648;
    border-radius:6px;padding:4px;cursor:pointer}
  #xijian-panel .xj-tabs button.active{color:#fff;border-color:#4b8bff;background:#1b2a44}
  #xijian-panel .xj-body{overflow:auto;padding:8px;flex:1}
  #xijian-panel .xj-foot{padding:6px 8px;border-top:1px solid #2c3648;color:#7f8ea6;font-size:11px}
  .xj-row{border:1px solid #26303f;border-radius:8px;padding:6px;margin-bottom:6px}
  .xj-row-head{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .xj-sub{margin:8px 0 4px;color:#9fb0c8;font-size:11px;letter-spacing:.04em}
  .xj-dim{color:#8fa0b8;font-size:11px}
  .xj-empty{color:#7f8ea6;font-size:11px;padding:8px 0}
  .xj-badge{font-size:10px;padding:1px 6px;border-radius:999px;border:1px solid}
  .xj-badge.ok{color:#7ee2a8;border-color:#2f6b4a}
  .xj-badge.empty{color:#9fd0ff;border-color:#2f5570}
  .xj-badge.warn{color:#ffd479;border-color:#6b5a2f}
  .xj-badge.bad{color:#ff9b9b;border-color:#6b2f2f}
  .xj-actions{display:flex;gap:6px;margin-top:4px}
  .xj-btn{background:#1b2a44;color:#dce8ff;border:1px solid #3a5a8a;border-radius:6px;
    padding:2px 8px;cursor:pointer;font-size:11px}
  .xj-btn.ghost{background:transparent;color:#9fb0c8}
  code{font-family:ui-monospace,Consolas,monospace;color:#b9c9e0}
  @media (max-width:900px){#xijian-panel{display:none}}
  `;
  document.head.appendChild(style);
}

function buildShell() {
  if (document.getElementById("xijian-panel")) return;
  const box = document.createElement("div");
  box.id = "xijian-panel";
  box.innerHTML = `
    <div class="xj-tabs">
      <button data-tab="rooms" class="active">房间三态</button>
      <button data-tab="trust">信任/撤销</button>
      <button data-tab="adapt">L5 自治</button>
    </div>
    <div class="xj-body" id="xj-body"><div class="xj-empty">加载中…</div></div>
    <div class="xj-foot" id="xj-foot">--</div>`;
  document.body.appendChild(box);
}

async function refresh() {
  const body = $("#xj-body");
  const foot = $("#xj-foot");
  if (!body) return;
  try {
    body.innerHTML = await RENDERERS[current]();
    foot.textContent = `更新于 ${new Date().toLocaleTimeString()} · 只读视图`;
  } catch (e) {
    body.innerHTML = `<div class="xj-empty">加载失败：${esc(e.message)}</div>`;
  }
}

function bindEvents() {
  document.addEventListener("click", async (ev) => {
    const tab = ev.target.closest("[data-tab]");
    if (tab) {
      current = tab.dataset.tab;
      document.querySelectorAll("#xijian-panel [data-tab]").forEach((b) =>
        b.classList.toggle("active", b === tab));
      refresh();
      return;
    }
    const undo = ev.target.closest("[data-undo]");
    if (undo) {
      undo.disabled = true;
      await postJSON(`/api/trust/undo/${undo.dataset.undo}`, {});
      refresh();
      return;
    }
    const accept = ev.target.closest("[data-accept]");
    if (accept) {
      await postJSON(`/api/adaptation/${accept.dataset.accept}/accept`, {});
      refresh();
      return;
    }
    const reject = ev.target.closest("[data-reject]");
    if (reject) {
      await postJSON(`/api/adaptation/${reject.dataset.reject}/reject`, {});
      refresh();
      return;
    }
  });
}

injectStyle();
buildShell();
bindEvents();
refresh();
setInterval(refresh, 5000);
