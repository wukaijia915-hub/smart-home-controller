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
        DigitalHuman.setStatus(dh.emotion === 'happy' ? '正在为您执行 😊' : '小智已收到');
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
      case 'alert':
        // 主动告警：数字人主动提示
        addMessage('assistant', '小智', '⚠️ ' + msg.message);
        DigitalHuman.setStatus('检测到异常，正在提醒');
        speakText(msg.message);
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

  window.addEventListener('DOMContentLoaded', init);
