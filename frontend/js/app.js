/* 前端主控：WebSocket 通信 + 3D 联动 + 对话 UI + 设备面板 + 传感器面板 + 语音输入/播报（ES Module） */
import HomeScene from './scene.js';
import DigitalHuman from './digital_human.js';

let ws;
const deviceMap = {};
let currentScene = 'home';
let ttsEnabled = true;
let currentAudio = null;
let recognition = null;
let isListening = false;
let pendingConfirmId = null;

const $ = (id) => document.getElementById(id);

// 数字人状态文案。原实现只有 'happy' / 其它 两种分支，于是环境告警
// （emotion='alert'）在界面上显示成"小智已收到"，与「检测到异常」的语义不符。
const DH_STATUS = {
  happy: '正在为您执行 😊',
  alert: '检测到异常，正在提醒',
  neutral: '小智已收到',
  speak: '小智已收到',
};

  // ---------- 初始化 ----------
  function init() {
    HomeScene.init($('scene3d'));
    DigitalHuman.init('dh-avatar');
    DigitalHuman.setStatus('正在连接中枢...');

    bindChat();
    bindScenes();
    bindConfirmModal();
    initSpeechRecognition();
    bindTtsToggle();
    initSchedule();
    initPerception();
    initInsight();
    connect();

    setTimeout(() => {
      addMessage('assistant', '小智', '你好，我是你的全屋智能中枢小智 🤖\n试试对我说："我回家了"、"看电影"、"空调调到24度"、"打开窗帘"。\n点击 🎤 可以语音输入，点击 🔊 可以开关语音播报。');
    }, 400);
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
      DigitalHuman.setStatus('已就绪，随时听候指令');
    };
    ws.onclose = () => {
      DigitalHuman.setStatus('连接断开，正在重连...');
      setTimeout(connect, 2000);
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        handleMessage(msg);
      } catch (e) { /* ignore */ }
    };
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case 'init':
        msg.devices.forEach((d) => applyDevice(d));
        renderSensors(msg.sensors);
        break;
      case 'device_state':
        applyDevice(msg.device);
        break;
      case 'sensor':
        renderSensors(msg.data);
        HomeScene.setEnvironment(msg.data);
        break;
      case 'agent_reply':
        addMessage('assistant', '小智', msg.reply);
        const dh = msg.digital_human || { emotion: 'happy' };
        DigitalHuman.setStatus(DH_STATUS[dh.emotion] || '小智已收到');
        speakText(msg.reply);
        renderTrace(msg.decision_trace);
        if (msg.requires_confirmation && msg.pending_id) {
          showConfirmModal(msg);
        }
        break;
      case 'confirm_result':
        if (msg.result && msg.result.ok) {
          addMessage('assistant', '小智', '✅ 已执行确认操作。');
        } else {
          addMessage('assistant', '小智', '⚠️ ' + ((msg.result && msg.result.error) || '确认失败'));
        }
        break;
      case 'scene':
        setSceneTag(msg.scene, msg.desc);
        break;
      case 'control_result':
        if (msg.result && msg.result.error) {
          addMessage('assistant', '小智', '⚠️ ' + msg.result.error);
        }
        break;
      case 'daily_summary':
        // 外环日报生成完毕（定时 22:00 或手动触发）：直接更新面板，不再播报，
        // 否则会和同一条内容的其它播报路径重复发声。
        renderDailySummary(msg.report);
        break;
      case 'perception_suggestion':

        // 感知层建议：**只是问句，不代表系统已执行任何设备动作**
        // （后端广播体带 actionable_by_system:false 明确这一点）
        addMessage('assistant', '小智', '💡 ' + msg.suggestion);
        DigitalHuman.setStatus('监测到情境变化，向您确认');
        speakText(msg.suggestion);
        loadPerception();
        break;
    }
  }

  // ---------- 语音播报（Edge-TTS，fallback 浏览器 SpeechSynthesis）----------
  async function speakText(text) {
    if (!ttsEnabled || !text) return;
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();

    try {
      const res = await fetch('/api/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw new Error('TTS API error');
      const blob = await res.blob();
      if (blob.size < 100) throw new Error('TTS empty');
      const url = URL.createObjectURL(blob);
      currentAudio = new Audio(url);
      currentAudio.onloadedmetadata = () => {
        DigitalHuman.speak(Math.min(8000, currentAudio.duration * 1000));
      };
      currentAudio.onended = () => {
        URL.revokeObjectURL(url);
        currentAudio = null;
      };
      currentAudio.onerror = () => {
        URL.revokeObjectURL(url);
        fallbackSpeak(text);
      };
      await currentAudio.play();
    } catch (e) {
      fallbackSpeak(text);
    }
  }

  function fallbackSpeak(text) {
    if (!('speechSynthesis' in window)) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = 'zh-CN';
    u.rate = 1.0;
    u.pitch = 1.0;
    DigitalHuman.speak(Math.min(6000, text.length * 120));
    window.speechSynthesis.speak(u);
  }

  function bindTtsToggle() {
    const btn = $('tts-toggle');
    if (!btn) return;
    btn.addEventListener('click', () => {
      ttsEnabled = !ttsEnabled;
      btn.classList.toggle('active', ttsEnabled);
      btn.textContent = ttsEnabled ? '🔊' : '🔇';
      if (!ttsEnabled && currentAudio) {
        currentAudio.pause();
        currentAudio = null;
      }
      if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    });
  }

  // ---------- 语音输入（Web Speech API）----------
  function initSpeechRecognition() {
    const micBtn = $('mic-btn');
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      if (micBtn) micBtn.style.display = 'none';
      return;
    }
    recognition = new SR();
    recognition.lang = 'zh-CN';
    recognition.interimResults = true;
    recognition.continuous = false;

    recognition.onresult = (e) => {
      let transcript = '';
      for (let i = 0; i < e.results.length; i++) {
        transcript += e.results[i][0].transcript;
      }
      $('chat-input').value = transcript;
    };

    recognition.onend = () => {
      isListening = false;
      if (micBtn) micBtn.classList.remove('listening');
      const text = $('chat-input').value.trim();
      if (text) sendMessage(text);
    };

    recognition.onerror = () => {
      isListening = false;
      if (micBtn) micBtn.classList.remove('listening');
    };

    if (micBtn) {
      micBtn.addEventListener('click', () => {
        if (isListening) {
          recognition.stop();
        } else {
          $('chat-input').value = '';
          recognition.start();
          isListening = true;
          micBtn.classList.add('listening');
        }
      });
    }
  }

  // ---------- 设备 ----------
  function applyDevice(device) {
    deviceMap[device.device_id] = device;
    HomeScene.updateDevice(device);
    renderDeviceList();
  }

  function describeDevice(d) {
    const a = d.attributes || {};
    const on = a.on ? a.on.value : false;
    switch (d.category) {
      case 'light': return on ? `亮度 ${a.brightness ? a.brightness.value : 0}%` : '已关闭';
      case 'curtain': return `开合 ${a.position ? a.position.value : 0}%`;
      case 'ac': return on ? `${a.temperature ? a.temperature.value : 26}℃ · ${a.mode ? a.mode.value : 'cool'}` : '已关闭';
      case 'tv': return on ? `音量 ${a.volume ? a.volume.value : 0}` : '已关闭';
      case 'fan': return on ? `${a.speed ? a.speed.value : 'mid'} 档` : '已关闭';
      case 'humidifier': return on ? `目标 ${a.target ? a.target.value : 55}%` : '已关闭';
      default: return d.state;
    }
  }

  function renderDeviceList() {
    const list = $('device-list');
    list.innerHTML = '';
    Object.values(deviceMap).forEach((d) => {
      const row = document.createElement('div');
      row.className = 'device-row';
      const on = d.attributes.on ? d.attributes.on.value : (d.category === 'curtain' ? (d.attributes.position && d.attributes.position.value > 0) : false);
      const stateCls = d.state !== 'online' ? 'off' : (on ? 'on' : 'off');
      const stateLabel = d.state === 'online' ? describeDevice(d) : (d.state === 'offline' ? '离线' : '故障');
      row.innerHTML = `
        <span class="d-name"><span class="d-dot ${stateCls}"></span>${d.name}</span>
        <span class="d-state">${stateLabel}</span>`;
      list.appendChild(row);
    });
  }

  // ---------- 传感器 ----------
  function renderSensors(s) {
    if (!s) return;
    const fmt = (v, unit) => (v === undefined || v === null ? '--' : `${Math.round(v * 10) / 10}${unit || ''}`);
    $('s-temp').textContent = fmt(s.temp_indoor, '℃');
    $('s-hum').textContent = fmt(s.humidity, '%');
    $('s-light').textContent = fmt(s.light_level, ' lux');
    $('s-motion').textContent = s.motion_living ? '有人' : '无人';
    $('s-motion2').textContent = s.motion_bedroom ? '有人' : '无人';
    $('s-air').textContent = fmt(s.air_quality, '');
  }

  // ---------- 决策链路 ----------
  function renderTrace(trace) {
    const list = $('trace-list');
    if (!trace || trace.length === 0) {
      if (!list.innerHTML) list.innerHTML = '<div class="trace-empty">等待指令…</div>';
      return;
    }
    list.innerHTML = '';
    trace.forEach((t) => {
      const row = document.createElement('div');
      row.className = 'trace-step';
      const badge = document.createElement('span');
      badge.className = `t-badge ${t.step}`;
      badge.textContent = t.step.toUpperCase();
      const detail = document.createElement('span');
      detail.className = 't-detail' + (t.ok === false ? ' fail' : '');
      detail.textContent = t.detail;
      row.appendChild(badge);
      row.appendChild(detail);
      list.appendChild(row);
    });
    list.scrollTop = list.scrollHeight;
  }

  // ---------- 高危确认弹窗 ----------
  function showConfirmModal(msg) {
    pendingConfirmId = msg.pending_id;
    const action = msg.pending_action || {};
    const desc = action.desc || action.command || '';
    const devName = action.device_name || action.device_id || '';
    $('confirm-text').textContent =
      `确认执行「${desc}」吗？目标设备：${devName}。\n该操作属于高危动作，已记录授权租约。`;
    $('confirm-modal').style.display = 'flex';
  }

  function bindConfirmModal() {
    $('confirm-ok').addEventListener('click', () => {
      if (pendingConfirmId && ws && ws.readyState === 1) {
        ws.send(JSON.stringify({ type: 'confirm', pending_id: pendingConfirmId }));
      }
      $('confirm-modal').style.display = 'none';
      pendingConfirmId = null;
    });
    $('confirm-cancel').addEventListener('click', () => {
      addMessage('assistant', '小智', '已取消该操作。');
      $('confirm-modal').style.display = 'none';
      pendingConfirmId = null;
    });
  }

  // ---------- 对话 ----------
  function bindChat() {
    const sendBtn = $('chat-send');
    const input = $('chat-input');
    sendBtn.addEventListener('click', () => {
      const text = input.value.trim();
      if (text) sendMessage(text);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const text = input.value.trim();
        if (text) sendMessage(text);
      }
    });
  }

  function sendMessage(text) {
    const input = $('chat-input');
    addMessage('user', '', text);
    input.value = '';
    if (ws && ws.readyState === 1) {
      DigitalHuman.setStatus('正在思考...');
      ws.send(JSON.stringify({ type: 'chat', text }));
    } else {
      addMessage('assistant', '小智', '⚠️ 连接已断开，请稍候重试。');
    }
  }

  function addMessage(role, who, text) {
    const log = $('chat-log');
    const div = document.createElement('div');
    div.className = `msg ${role}`;
    div.innerHTML = role === 'assistant' ? `<span class="who">${who}</span>${escapeHtml(text)}` : escapeHtml(text);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ---------- 场景 ----------
  function bindScenes() {
    document.querySelectorAll('#scene-buttons button').forEach((btn) => {
      btn.addEventListener('click', () => {
        const scene = btn.dataset.scene;
        if (ws && ws.readyState === 1) {
          ws.send(JSON.stringify({ type: 'scene', scene }));
        }
        setSceneTag(scene);
      });
    });
  }

  function setSceneTag(scene, desc) {
    currentScene = scene;
    const tag = $('scene-tag');
    if (desc) {
      tag.textContent = desc;
    } else {
      const map = { home: '居家', away: '离家', movie: '观影', sleep: '睡眠', morning: '起床', guest: '会客', save: '节能' };
      tag.textContent = map[scene] || '居家';
    }
    document.querySelectorAll('#scene-buttons button').forEach((b) => {
      b.classList.toggle('active', b.dataset.scene === scene);
    });
  }

  // ---------- 定时任务 ----------
  function initSchedule() {
    loadSchedule();
    $('sch-add-btn').addEventListener('click', () => {
      const name = $('sch-name').value.trim() || '定时任务';
      const time = $('sch-time').value || '07:00';
      const [hour, minute] = time.split(':');
      const scene = $('sch-action').value;
      fetch('/api/schedule/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, hour: parseInt(hour), minute: parseInt(minute), action_type: 'scene', scene }),
      }).then(r => r.json()).then(data => {
        if (data.ok) { $('sch-name').value = ''; loadSchedule(); }
      });
    });
  }

  function loadSchedule() {
    fetch('/api/schedule/tasks').then(r => r.json()).then(data => {
      const list = $('schedule-list');
      if (!data.tasks || data.tasks.length === 0) {
        list.innerHTML = '<span style="color:var(--text-dim);">暂无定时任务</span>';
        return;
      }
      list.innerHTML = data.tasks.map(t => {
        const time = `${String(t.hour).padStart(2,'0')}:${String(t.minute).padStart(2,'0')}`;
        const sceneMap = { home: '回家', away: '离家', movie: '观影', sleep: '睡眠', morning: '起床', guest: '会客', save: '节能' };
        const action = t.action_type === 'scene' ? (sceneMap[t.scene] || t.scene) : '设备控制';
        return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--panel-border);">
          <span>${t.name} <span style="color:var(--accent);">${time}</span> → ${action}</span>
          <button onclick="delSchedule('${t.id}')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:12px;">删除</button>
        </div>`;
      }).join('');
    });
  }

  window.delSchedule = function(id) {
    fetch(`/api/schedule/tasks/${id}`, { method: 'DELETE' }).then(() => loadSchedule());
  };

  // ---------- 家庭情境看板（白盒参数，可展开原始依据）----------
  // 展示感知层的判断与它的**依据**：每个参数都能点开看到真值来源、计算方式与原始数据。
  // 这是把"可解释性"从口号变成可查证据的地方，也是答辩时最该演示的面板。
  let sitExpanded = {};        // 记住展开状态，避免轮询刷新时收起

  function initPerception() {
    loadPerception();
    setInterval(loadPerception, 10000);
  }

  function loadPerception() {
    fetch('/api/perception')
      .then((r) => r.json())
      .then((d) => { if (d && d.situation) renderPerception(d); })
      .catch(() => { /* 感知层不可用不应影响其它面板 */ });
  }

  function fmtParamValue(p) {
    const v = typeof p.value === 'number' ? Math.round(p.value * 100) / 100 : p.value;
    const unit = (p.unit && p.unit.length <= 4) ? p.unit : '';
    return `${v}${unit}`;
  }

  function paramDetailHtml(p) {
    const ev = (p.evidence && Object.keys(p.evidence).length)
      ? JSON.stringify(p.evidence, null, 1) : '（无）';
    const note = p.note ? `<span class="sit-note">⚠ ${escapeHtml(p.note)}</span>\n` : '';
    return `${note}<b>真值来源</b>：${escapeHtml(p.truth || '-')}\n`
         + `<b>计算依据</b>：${escapeHtml(p.source || '-')}\n`
         + `<b>置信度</b>：${(p.confidence != null ? p.confidence : 1)}\n`
         + `<b>原始数据</b>：${escapeHtml(ev)}`;
  }

  function renderPerception(d) {
    const sit = d.situation || {};
    const tag = $('sit-region');
    if (tag) {
      tag.textContent = (sit.region_label || '--').split('（')[0];
      tag.className = 'sit-tag lv-' + (sit.region_level || 'info');
    }

    const box = $('sit-params');
    if (!box) return;
    const params = sit.params || {};
    box.innerHTML = Object.keys(params).map((k) => {
      const p = params[k];
      const pct = Math.round((p.normalized || 0) * 100);
      const lowConf = (p.confidence != null && p.confidence < 1);
      const detail = sitExpanded[k] ? `<div class="sit-detail">${paramDetailHtml(p)}</div>` : '';
      return `<div class="sit-row${lowConf ? ' low-conf' : ''}" data-param="${k}" title="点击查看原始依据">
          <span class="sit-name">${escapeHtml(p.label)}</span>
          <span class="sit-bar"><i style="width:${pct}%"></i></span>
          <span class="sit-val">${fmtParamValue(p)}</span>
        </div>${detail}`;
    }).join('');

    box.querySelectorAll('.sit-row').forEach((row) => {
      row.addEventListener('click', () => {
        const k = row.getAttribute('data-param');
        sitExpanded[k] = !sitExpanded[k];
        renderPerception(d);
      });
    });

    const sug = $('sit-suggestion');
    if (!sug) return;
    if (d.suggestion) {
      sug.style.display = 'block';
      sug.innerHTML = `<span class="sit-sug-badge">建议 · 待您确认</span>`
        + `${escapeHtml(d.suggestion)}`
        + `<span class="sit-sug-basis">依据：${escapeHtml(d.suggestion_basis || '')}</span>`;
    } else {
      sug.style.display = 'none';
    }
  }

  // ---------- 记忆与观测面板 ----------
  // 分层延迟 + 经验库 + 外环日报。
  // 关键点是**分层**：规则层（纯内存查表，毫秒）与慢环（模型生成，秒）必须分开显示。
  // 把两者混成一个"平均响应时间"，得到的数字既不解释性能也不解释体验。
  let insTimer = null;

  function initInsight() {
    loadInsight();
    loadDailySummary();
    insTimer = setInterval(loadInsight, 15000);
    const btn = $('ins-sum-btn');
    if (btn) {
      btn.addEventListener('click', () => {
        btn.disabled = true;
        btn.textContent = '生成中…';
        fetch('/api/jobs/daily_summary/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ window_hours: 24 }),
        })
          .then((r) => r.json())
          .then((d) => { if (d.report) renderDailySummary(d.report); })
          .catch(() => {})
          .finally(() => { btn.disabled = false; btn.textContent = '生成'; });
      });
    }
  }

  function loadInsight() {
    fetch('/api/metrics/llm')
      .then((r) => r.json())
      .then((d) => {
        const rule = d.rule_path || {};
        const slow = d.slow_path || {};
        const calls = d.llm_calls || {};
        setText('ins-rule', `${fmtMs(rule.avg_ms)} / ${fmtMs(rule.p95_ms)}`);
        setText('ins-slow', slow.samples
          ? `${fmtMs(slow.avg_ms)} / ${fmtMs(slow.p95_ms)}`
          : '暂无慢环样本');
        setText('ins-degrade', calls.calls
          ? `${Math.round((calls.degrade_rate || 0) * 100)}%　调用 ${calls.calls} 次`
          : '尚无调用');
        const tag = $('ins-src-tag');
        if (tag) {
          const n = (rule.samples || 0) + (slow.samples || 0);
          tag.textContent = `样本 ${n}`;
        }
      })
      .catch(() => { /* 观测层不可用不应影响其它面板 */ });

    fetch('/api/memory/stats')
      .then((r) => r.json())
      .then((d) => {
        const e = d.experience || {};
        setText('ins-exp', `${e.total || 0} 条 / ${e.avg_rating || 0} 分`);
      })
      .catch(() => {});
  }

  function loadDailySummary() {
    fetch('/api/jobs/daily_summary/latest')
      .then((r) => r.json())
      .then((d) => { if (d.report) renderDailySummary(d.report); })
      .catch(() => {});
  }

  function renderDailySummary(report) {
    const body = $('ins-sum-body');
    if (!body || !report) return;
    body.textContent = report.narrative || '（空）';
    const tag = $('ins-sum-src');
    if (tag) {
      // 如实标注这一份是模型写的还是模板兜底的 —— 断网演练时用户看到的是模板文案，
      // 系统不该假装自己用了大模型。
      const viaLlm = report.source === 'llm';
      tag.textContent = viaLlm ? 'LLM 生成' : '模板降级';
      tag.className = 'ins-tag ' + (viaLlm ? 'lv-llm' : 'lv-template');
      tag.title = viaLlm
        ? `后端：${(report.llm && report.llm.backend) || '-'}`
        : `原因：${(report.llm && report.llm.degrade_reason) || '模型不可用'}`;
    }
  }

  function setText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;
  }

  function fmtMs(ms) {
    if (!ms) return '0ms';
    return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
  }

  window.addEventListener('DOMContentLoaded', init);
