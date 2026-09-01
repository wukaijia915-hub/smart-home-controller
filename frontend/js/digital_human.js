/* 数字人：图片形象 + 说话/思考动画 + 上传定制（ES Module）
 * 默认使用机器人吉祥物，支持预设形象切换和用户上传图片定制（localStorage 持久化）。
 */
const DigitalHuman = {};
let avatarEl, imgEl;
const STORAGE_KEY = 'lingxi_avatar_custom';
const PRESET_KEY = 'lingxi_avatar_preset';
const DEFAULT_AVATAR = 'assets/avatar_robot.png';

const PRESETS = {
  robot: { src: 'assets/avatar_robot.png', name: '小智' },
  male: { src: 'assets/avatar_male.png', name: '小灵' },
};

DigitalHuman.init = function (containerId) {
    const holder = document.getElementById(containerId);
    if (!holder) return;
    holder.innerHTML = '';

    const ring = document.createElement('div');
    ring.className = 'dh-ring';

    imgEl = document.createElement('img');
    imgEl.className = 'dh-img';
    imgEl.alt = '数字人';
    imgEl.draggable = false;

    // 优先级：用户定制 > 预设选择 > 默认
    const savedCustom = localStorage.getItem(STORAGE_KEY);
    const savedPreset = localStorage.getItem(PRESET_KEY) || 'robot';
    imgEl.src = savedCustom || (PRESETS[savedPreset]?.src || DEFAULT_AVATAR);

    holder.appendChild(ring);
    holder.appendChild(imgEl);
    avatarEl = holder;
    avatarEl.classList.add('idle');

    // 绑定预设形象按钮
    document.querySelectorAll('.dh-preset').forEach(btn => {
      btn.addEventListener('click', () => {
        const type = btn.dataset.avatar;
        DigitalHuman.setPreset(type);
        document.querySelectorAll('.dh-preset').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
      });
    });
    // 恢复预设按钮选中状态
    if (!savedCustom) {
      const activeBtn = document.querySelector(`.dh-preset[data-avatar="${savedPreset}"]`);
      if (activeBtn) {
        document.querySelectorAll('.dh-preset').forEach(b => b.classList.remove('active'));
        activeBtn.classList.add('active');
      }
    }

    // 绑定上传按钮
    const uploadBtn = document.getElementById('dh-upload-btn');
    const fileInput = document.getElementById('dh-avatar-input');
    const resetBtn = document.getElementById('dh-reset-btn');
    if (uploadBtn && fileInput) {
      uploadBtn.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (!file) return;
        if (!file.type.startsWith('image/')) {
          alert('请选择图片文件');
          return;
        }
        if (file.size > 5 * 1024 * 1024) {
          alert('图片大小不能超过 5MB');
          return;
        }
        const reader = new FileReader();
        reader.onload = (ev) => {
          const dataUrl = ev.target.result;
          imgEl.src = dataUrl;
          try {
            localStorage.setItem(STORAGE_KEY, dataUrl);
          } catch (err) {
            console.warn('头像保存失败（可能超出 localStorage 容量）:', err);
          }
          document.querySelectorAll('.dh-preset').forEach(b => b.classList.remove('active'));
        };
        reader.readAsDataURL(file);
        fileInput.value = '';
      });
    }
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        localStorage.removeItem(STORAGE_KEY);
        DigitalHuman.setPreset('robot');
        document.querySelectorAll('.dh-preset').forEach(b => b.classList.remove('active'));
        document.querySelector('.dh-preset[data-avatar="robot"]')?.classList.add('active');
      });
    }
  };

  /** 切换预设形象 */
DigitalHuman.setPreset = function (type) {
    if (!imgEl) return;
    const preset = PRESETS[type];
    if (!preset) return;
    localStorage.removeItem(STORAGE_KEY);
    localStorage.setItem(PRESET_KEY, type);
    imgEl.src = preset.src;
  };

  /** 切换预设形象（兼容旧接口） */
DigitalHuman.setAvatar = function (type) {
    DigitalHuman.setPreset(type);
  };

  /** 说话动画：持续 durationMs */
DigitalHuman.speak = function (durationMs) {
    if (!avatarEl) return;
    avatarEl.classList.remove('idle', 'thinking');
    avatarEl.classList.add('speaking');
    clearTimeout(DigitalHuman._speakTimer);
    DigitalHuman._speakTimer = setTimeout(() => {
      avatarEl.classList.remove('speaking');
      avatarEl.classList.add('idle');
    }, durationMs || 2000);
  };

  DigitalHuman.setStatus = function (text) {
    const el = document.getElementById('dh-status');
    if (el) el.textContent = text;
    if (!avatarEl) return;
    if (text && (text.includes('思考') || text.includes('执行'))) {
      avatarEl.classList.remove('idle', 'speaking');
      avatarEl.classList.add('thinking');
    } else if (!avatarEl.classList.contains('speaking')) {
      avatarEl.classList.remove('thinking');
      avatarEl.classList.add('idle');
    }
  };

export default DigitalHuman;
