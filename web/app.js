/* =========================================================
   Velox — Game Optimizer UI
   Мост к Python: window.pywebview.api.*
   Если pywebview не найден (открыто в обычном браузере) —
   всё работает на mock-данных.

   Обязательные методы бэкенда (из ТЗ):
     get_system_stats()                    -> dict (схема ниже)
     apply_vibrance(level)                 -> {ok, message?}
     apply_resolution(width, height, hz)   -> {ok, message?}
     reset_resolution()                    -> {ok, message?}
     apply_game_preset(game_id, preset)    -> {ok, message?}
     toggle_fps_overlay(enabled)           -> {ok}
     minimize_window(), close_window()

   Необязательные (вызываются, только если есть в API):
     turbo_boost()                         -> {ok, freed_mb, priority_process}
     get_installed_games()                 -> {fortnite: {found, path}, ...}
     backup_game_config(game_id)           -> {ok, path}
     apply_game_options(game_id, options)  -> {ok}
     set_vibrance_auto(enabled, level)     -> {ok}
     set_scaling_mode(stretch)             -> {ok}

   Схема get_system_stats():
   {
     cpu:  { name, load, temp, clock_mhz, cores, threads },
     gpu:  { name, load, temp, clock_mhz, vram_used, vram_total },   // ГБ
     ram:  { used, total },                                          // ГБ
     fps:  { current, avg, low1, game } | null,
     display: { width, height, hz }
   }
   Любое поле может быть null — интерфейс покажет «—».
   game_id: fortnite | cs2 | rust | valorant
   preset:  esports | potato | default
   ========================================================= */
(() => {
  'use strict';

  /* ---------------- Утилиты ---------------- */
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const mean = (a) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : 0);
  const nf1 = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const nf0 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
  const gb = (v) => (isNum(v) ? nf1.format(v) : '—');
  const fmtSize = (mb) => (!isNum(mb) ? '—' : mb >= 1024 ? `${nf1.format(mb / 1024)} ГБ` : `${nf0.format(mb)} МБ`);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const store = {
    get(key, def) {
      try {
        const v = localStorage.getItem('velox.' + key);
        return v === null ? def : JSON.parse(v);
      } catch (e) { return def; }
    },
    set(key, val) {
      try { localStorage.setItem('velox.' + key, JSON.stringify(val)); } catch (e) { /* хранилище недоступно */ }
    },
  };

  const hexToRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const mix = (a, b, t) => {
    const pa = hexToRgb(a), pb = hexToRgb(b);
    return `rgb(${pa.map((v, i) => Math.round(v + (pb[i] - v) * t)).join(',')})`;
  };
  const rgba = (h, a) => `rgba(${hexToRgb(h).join(',')},${a})`;

  function setLoading(btn, on) {
    btn.classList.toggle('is-loading', on);
    btn.setAttribute('aria-busy', on ? 'true' : 'false');
  }

  /* ---------------- Тосты ---------------- */
  const TOAST_ICON = {
    ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    err: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M12 6.5v7"/><path d="M12 17.5h.01"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M12 10.5v7"/><path d="M12 6.5h.01"/></svg>',
  };

  function toast(text, type = 'ok', ms = 3400) {
    const box = $('#toasts');
    while (box.children.length >= 4) box.firstElementChild.remove();
    const el = document.createElement('div');
    el.className = `toast toast--${type}`;
    el.setAttribute('role', type === 'err' ? 'alert' : 'status');
    el.innerHTML = `<span class="toast__icon">${TOAST_ICON[type] || TOAST_ICON.info}</span><span class="toast__text"></span>`;
    el.querySelector('.toast__text').textContent = text;
    box.appendChild(el);
    setTimeout(() => {
      el.classList.add('is-leaving');
      el.addEventListener('animationend', () => el.remove(), { once: true });
    }, type === 'err' ? ms + 2000 : ms);
  }

  const errText = (res, fallback) => (res && (res.error || res.message)) || fallback;

  /* =========================================================
     Mock-бэкенд для превью в браузере
     ========================================================= */
  const M = { cpu: 22, cpuT: 58, gpu: 82, gpuT: 66, vram: 6.4, ram: 13.6, fpsBase: 300, fpsHist: [] };
  const walk = (v, step, min, max) => clamp(v + (Math.random() - 0.5) * 2 * step, min, max);

  const Mock = {
    async get_system_stats() {
      M.cpu = walk(M.cpu, 7, 9, 58);
      M.cpuT = clamp(M.cpuT + (46 + M.cpu * 0.45 - M.cpuT) * 0.25 + (Math.random() - 0.5) * 1.5, 38, 95);
      M.gpu = walk(M.gpu, 6, 55, 99);
      M.gpuT = clamp(M.gpuT + (48 + M.gpu * 0.24 - M.gpuT) * 0.2 + (Math.random() - 0.5), 38, 92);
      M.vram = walk(M.vram, 0.15, 5.2, 8.1);
      M.ram = walk(M.ram, 0.12, 6, 22);
      M.fpsBase = walk(M.fpsBase, 9, 250, 340);
      let cur = M.fpsBase + (Math.random() - 0.5) * 24;
      if (Math.random() < 0.05) cur -= 70 + Math.random() * 60;
      cur = Math.round(clamp(cur, 120, 400));
      M.fpsHist.push(cur);
      if (M.fpsHist.length > 240) M.fpsHist.shift();
      const sorted = [...M.fpsHist].sort((a, b) => a - b);
      const low = mean(sorted.slice(0, Math.max(1, Math.floor(sorted.length * 0.01))));
      return {
        cpu: { name: 'AMD Ryzen 7 7800X3D', load: Math.round(M.cpu), temp: Math.round(M.cpuT), clock_mhz: Math.round(4850 + (Math.random() - 0.5) * 120), cores: 8, threads: 16 },
        gpu: { name: 'NVIDIA GeForce RTX 4070 SUPER', load: Math.round(M.gpu), temp: Math.round(M.gpuT), clock_mhz: Math.round(2655 + (Math.random() - 0.5) * 80), vram_used: +M.vram.toFixed(1), vram_total: 12 },
        ram: { used: +M.ram.toFixed(1), total: 32 },
        fps: { current: cur, avg: Math.round(mean(M.fpsHist)), low1: Math.round(low), game: 'Counter-Strike 2' },
        display: { width: 1920, height: 1080, hz: 240 },
      };
    },
    async apply_vibrance(level) { await sleep(260); return { ok: true, level }; },
    async apply_resolution(w, h, hz) { await sleep(700); return { ok: true, message: `${w}×${h}, ${hz} Гц` }; },
    async reset_resolution() { await sleep(500); return { ok: true }; },
    async apply_game_preset() { await sleep(850); return { ok: true }; },
    async apply_game_options() { await sleep(200); return { ok: true }; },
    async toggle_fps_overlay(enabled) { await sleep(220); return { ok: true, enabled }; },
    async get_overlay_settings() { return { ok: true, position: 'top_left', offset_x: 16, offset_y: 16, dpi: 800, nickname: 'Player', keys: ['W', 'A', 'S', 'D', 'Shift', 'Space'], image_path: null }; },
    async set_overlay_settings(settings) { return { ok: true, ...settings }; },
    async choose_overlay_image() { return { ok: false, error: 'Выбор файла доступен в десктопной программе' }; },
    async clear_overlay_image() { return { ok: true }; },
    async turbo_boost() {
      await sleep(1500);
      const freed = 0.7 + Math.random() * 1.3;
      M.ram = Math.max(6, M.ram - freed);
      return { ok: true, freed_mb: Math.round(freed * 1024), priority_process: 'cs2.exe' };
    },
    async backup_game_config(id) {
      await sleep(650);
      const d = new Date();
      const p = (n) => String(n).padStart(2, '0');
      return { ok: true, path: `backups\\${id}\\${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}-${p(d.getMinutes())}` };
    },
    async get_installed_games() {
      await sleep(450);
      return {
        fortnite: { found: true, path: 'C:\\Program Files\\Epic Games\\Fortnite' },
        cs2: { found: true, path: 'D:\\SteamLibrary\\steamapps\\common\\Counter-Strike Global Offensive' },
        rust: { found: false, path: null },
        valorant: { found: true, path: 'C:\\Riot Games\\VALORANT\\live' },
      };
    },
    async set_vibrance_auto() { await sleep(150); return { ok: true }; },
    async set_scaling_mode() { await sleep(150); return { ok: true }; },
    async minimize_window() { toast('Свернуть окно можно внутри pywebview. Сейчас открыт браузерный превью-режим.', 'info'); return { ok: true }; },
    async close_window() { toast('Закрыть окно можно внутри pywebview. Сейчас открыт браузерный превью-режим.', 'info'); return { ok: true }; },
  };

  /* =========================================================
     Мост к pywebview
     ========================================================= */
  function normalize(res) {
    if (res === undefined || res === null || res === true) return { ok: true };
    if (res === false) return { ok: false };
    if (typeof res === 'object' && !Array.isArray(res)) return 'ok' in res ? res : { ok: true, ...res };
    return { ok: true, value: res };
  }

  const Bridge = {
    live: false,
    api() { return window.pywebview && window.pywebview.api ? window.pywebview.api : null; },
    has(method) { const a = this.api(); return !!a && typeof a[method] === 'function'; },
    async call(method, ...args) {
      if (this.live) {
        if (!this.has(method)) return { ok: false, unsupported: true, error: `В бэкенде нет метода ${method}()` };
        try {
          return normalize(await this.api()[method](...args));
        } catch (e) {
          return { ok: false, error: (e && e.message) || String(e) };
        }
      }
      if (typeof Mock[method] !== 'function') return { ok: false, unsupported: true, error: `Нет mock для ${method}()` };
      return normalize(await Mock[method](...args));
    },
  };

  function setLive(live) {
    Bridge.live = live;
    const conn = $('#connStatus');
    conn.classList.toggle('is-live', live);
    $('#connTitle').textContent = live ? 'Бэкенд подключён' : 'Демо-режим';
    $('#connSub').textContent = live ? 'Данные с этого ПК' : 'Открыто в браузере, данные тестовые';
    $('#livePill').classList.toggle('is-demo', !live);
    $('#liveText').textContent = live ? 'Обновляется каждую секунду' : 'Демо-данные';
  }

  function waitForBridge(timeout = 1600) {
    return new Promise((resolve) => {
      if (window.pywebview && window.pywebview.api) return resolve(true);
      const t = setTimeout(() => resolve(false), timeout);
      window.addEventListener('pywebviewready', () => { clearTimeout(t); resolve(true); }, { once: true });
    });
  }

  /* =========================================================
     Навигация
     ========================================================= */
  const Nav = {
    current: null,
    init() {
      this.items = $$('.nav__item');
      this.ind = $('.nav__indicator');
      this.items.forEach((b) => b.addEventListener('click', () => this.go(b.dataset.page)));
      $('.nav').addEventListener('keydown', (e) => {
        if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
        e.preventDefault();
        const i = this.items.findIndex((b) => b.dataset.page === this.current);
        let n = i;
        if (e.key === 'ArrowDown') n = (i + 1) % this.items.length;
        if (e.key === 'ArrowUp') n = (i - 1 + this.items.length) % this.items.length;
        if (e.key === 'Home') n = 0;
        if (e.key === 'End') n = this.items.length - 1;
        this.go(this.items[n].dataset.page);
        this.items[n].focus();
      });
      document.addEventListener('keydown', (e) => {
        if (e.ctrlKey && !e.shiftKey && !e.altKey && /^[1-4]$/.test(e.key)) {
          e.preventDefault();
          this.go(this.items[+e.key - 1].dataset.page);
        }
      });
      window.addEventListener('resize', () => this.move(true));
      this.go(store.get('page', 'dashboard'), true);
    },
    go(page, instant = false) {
      if (!$('#page-' + page)) page = 'dashboard';
      if (page === this.current) return;
      this.current = page;
      this.items.forEach((b) => {
        const on = b.dataset.page === page;
        b.classList.toggle('is-active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
        b.tabIndex = on ? 0 : -1;
      });
      $$('.page').forEach((p) => p.classList.toggle('is-active', p.id === 'page-' + page));
      $('#main').scrollTop = 0;
      this.move(instant);
      store.set('page', page);
      document.dispatchEvent(new CustomEvent('pagechange', { detail: page }));
    },
    move(instant) {
      const b = this.items.find((i) => i.classList.contains('is-active'));
      if (!b) return;
      if (instant) this.ind.style.transition = 'none';
      this.ind.style.transform = `translateY(${b.offsetTop}px)`;
      this.ind.style.height = `${b.offsetHeight}px`;
      if (instant) { void this.ind.offsetHeight; this.ind.style.transition = ''; }
    },
  };

  /* =========================================================
     Канвас-графики
     ========================================================= */
  function fitCanvas(c) {
    const r = c.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    const dpr = window.devicePixelRatio || 1;
    const W = Math.round(r.width * dpr), H = Math.round(r.height * dpr);
    if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    c._w = r.width; c._h = r.height;
    return ctx;
  }

  function smoothPath(ctx, pts) {
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length - 1; i++) {
      const xc = (pts[i][0] + pts[i + 1][0]) / 2;
      const yc = (pts[i][1] + pts[i + 1][1]) / 2;
      ctx.quadraticCurveTo(pts[i][0], pts[i][1], xc, yc);
    }
    const last = pts[pts.length - 1];
    ctx.lineTo(last[0], last[1]);
  }

  const SPARK_LEN = 60;
  const FPS_LEN = 90;

  function drawSpark(canvas, data, max, c0, c1) {
    const ctx = fitCanvas(canvas);
    if (!ctx) return;
    const w = canvas._w, h = canvas._h;
    ctx.clearRect(0, 0, w, h);
    if (data.length < 2) return;
    const step = w / (SPARK_LEN - 1);
    const pts = data.map((v, i) => [(SPARK_LEN - data.length + i) * step, h - 3 - (clamp(v, 0, max) / max) * (h - 8)]);

    const fill = ctx.createLinearGradient(0, 0, 0, h);
    fill.addColorStop(0, rgba(c0, 0.26));
    fill.addColorStop(1, rgba(c0, 0));
    ctx.beginPath();
    smoothPath(ctx, pts);
    ctx.lineTo(pts[pts.length - 1][0], h);
    ctx.lineTo(pts[0][0], h);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();

    const line = ctx.createLinearGradient(0, 0, w, 0);
    line.addColorStop(0, rgba(c0, 0.5));
    line.addColorStop(1, c1);
    ctx.beginPath();
    smoothPath(ctx, pts);
    ctx.strokeStyle = line;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    ctx.stroke();

    const l = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(l[0] - 2, l[1], 2.6, 0, Math.PI * 2);
    ctx.fillStyle = c1;
    ctx.shadowColor = c1;
    ctx.shadowBlur = 8;
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  function drawFps(canvas, data, emptyMsg) {
    const ctx = fitCanvas(canvas);
    if (!ctx) return;
    const w = canvas._w, h = canvas._h;
    ctx.clearRect(0, 0, w, h);

    if (data.length < 2) {
      ctx.fillStyle = '#687089';
      ctx.font = '500 12.5px Inter, "Segoe UI", sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(emptyMsg || 'Запустите игру, и здесь появится график FPS', w / 2, h / 2);
      return;
    }

    const padT = 14, padB = 14, padL = 12, padR = 46;
    const gw = w - padL - padR, gh = h - padT - padB;
    const max = Math.max(60, Math.ceil((Math.max(...data) * 1.15) / 50) * 50);

    ctx.lineWidth = 1;
    ctx.font = '500 10.5px "JetBrains Mono", Consolas, monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (let i = 0; i <= 3; i++) {
      const y = Math.round(padT + (gh * i) / 3) + 0.5;
      ctx.strokeStyle = 'rgba(255,255,255,.05)';
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + gw, y);
      ctx.stroke();
      ctx.fillStyle = '#5D6479';
      ctx.fillText(String(Math.round(max * (1 - i / 3))), padL + gw + 8, y);
    }

    const step = gw / (FPS_LEN - 1);
    const pts = data.map((v, i) => [padL + (FPS_LEN - data.length + i) * step, padT + gh - (clamp(v, 0, max) / max) * gh]);

    const fill = ctx.createLinearGradient(0, padT, 0, padT + gh);
    fill.addColorStop(0, 'rgba(76,110,255,.30)');
    fill.addColorStop(1, 'rgba(76,110,255,0)');
    ctx.beginPath();
    smoothPath(ctx, pts);
    ctx.lineTo(pts[pts.length - 1][0], padT + gh);
    ctx.lineTo(pts[0][0], padT + gh);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();

    const avg = mean(data);
    const ay = Math.round(padT + gh - (avg / max) * gh) + 0.5;
    ctx.setLineDash([4, 5]);
    ctx.strokeStyle = 'rgba(30,216,160,.55)';
    ctx.beginPath();
    ctx.moveTo(padL, ay);
    ctx.lineTo(padL + gw, ay);
    ctx.stroke();
    ctx.setLineDash([]);

    const line = ctx.createLinearGradient(padL, 0, padL + gw, 0);
    line.addColorStop(0, '#2F80FF');
    line.addColorStop(1, '#8B5CF6');
    ctx.beginPath();
    smoothPath(ctx, pts);
    ctx.strokeStyle = line;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.stroke();

    const l = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(l[0], l[1], 3.5, 0, Math.PI * 2);
    ctx.fillStyle = '#fff';
    ctx.shadowColor = '#7D8BFF';
    ctx.shadowBlur = 12;
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  /* =========================================================
     Дашборд
     ========================================================= */
  const SEGMENTS = 24;

  function buildSegbar(el, from, to) {
    el.innerHTML = '';
    for (let i = 0; i < SEGMENTS; i++) {
      const s = document.createElement('i');
      s.style.setProperty('--c', mix(from, to, i / (SEGMENTS - 1)));
      el.appendChild(s);
    }
  }

  function setSegbar(el, pct) {
    const p = isNum(pct) ? clamp(pct, 0, 100) : 0;
    const n = Math.round((p / 100) * SEGMENTS);
    Array.from(el.children).forEach((s, i) => s.classList.toggle('on', i < n));
    el.classList.toggle('is-high', p >= 92);
  }

  function setTemp(key, t) {
    const el = $(`[data-temp="${key}"]`);
    el.querySelector('.mono').textContent = isNum(t) ? Math.round(t) : '—';
    el.dataset.level = !isNum(t) ? '' : t >= 85 ? 'hot' : t >= 72 ? 'warm' : 'ok';
  }

  const text = (sel, v) => { const el = $(sel); if (el.textContent !== String(v)) el.textContent = v; };

  const Dash = {
    hist: { cpu: [], gpu: [], ram: [], fps: [] },
    last: null,

    init() {
      buildSegbar($('#cpuBar'), '#2F80FF', '#6C6BFF');
      buildSegbar($('#gpuBar'), '#7A63FF', '#A77BFF');
      buildSegbar($('#ramBar'), '#1ED8A0', '#2F9BFF');

      const ov = $('#overlayToggle');
      ov.checked = !!store.get('overlay', false);
      $('#overlayMini').classList.toggle('is-on', ov.checked);
      ov.addEventListener('change', () => this.toggleOverlay(ov));

      $('#turboBtn').addEventListener('click', () => Turbo.run());

      document.addEventListener('pagechange', (e) => { if (e.detail === 'dashboard') this.drawCharts(); });
      window.addEventListener('resize', () => this.drawCharts());
    },

    push(arr, v, len) {
      arr.push(isNum(v) ? v : 0);
      if (arr.length > len) arr.shift();
    },

    render(s) {
      this.last = s;
      const c = s.cpu || {}, g = s.gpu || {}, r = s.ram || {}, f = s.fps;

      // CPU
      text('#cpuName', c.name || 'Процессор');
      text('#cpuLoad', isNum(c.load) ? Math.round(c.load) : '—');
      setSegbar($('#cpuBar'), c.load);
      setTemp('cpu', c.temp);
      text('#cpuClock', isNum(c.clock_mhz) ? `${(c.clock_mhz / 1000).toFixed(2).replace('.', ',')} ГГц` : '—');
      text('#cpuCores', isNum(c.cores) ? `${c.cores} / ${isNum(c.threads) ? c.threads : c.cores}` : '—');

      // GPU
      text('#gpuName', g.name || 'Видеокарта');
      text('#gpuLoad', isNum(g.load) ? Math.round(g.load) : '—');
      setSegbar($('#gpuBar'), g.load);
      setTemp('gpu', g.temp);
      text('#gpuClock', isNum(g.clock_mhz) ? `${nf0.format(g.clock_mhz)} МГц` : '—');
      text('#gpuVram', isNum(g.vram_used) && isNum(g.vram_total) ? `${gb(g.vram_used)} / ${nf0.format(g.vram_total)} ГБ` : '—');

      // RAM
      const ramPct = isNum(r.used) && isNum(r.total) && r.total > 0 ? (r.used / r.total) * 100 : null;
      text('#ramUsed', gb(r.used));
      text('#ramTotal', isNum(r.total) ? `/ ${nf0.format(r.total)} ГБ` : '/ — ГБ');
      text('#ramName', isNum(r.total) ? `${nf0.format(r.total)} ГБ установлено` : 'ОЗУ');
      setSegbar($('#ramBar'), ramPct);
      text('#ramFree', isNum(r.used) && isNum(r.total) ? `${gb(Math.max(0, r.total - r.used))} ГБ` : '—');
      text('#ramPct', isNum(ramPct) ? `${Math.round(ramPct)}%` : '—');

      this.push(this.hist.cpu, c.load, SPARK_LEN);
      this.push(this.hist.gpu, g.load, SPARK_LEN);
      this.push(this.hist.ram, ramPct, SPARK_LEN);

      // FPS
      const running = !!(f && isNum(f.current));
      const gameName = f && f.game ? f.game : null;
      $('#fpsGame').classList.toggle('is-running', running || !!gameName);
      text('#fpsGameName', gameName || (running ? 'Игра запущена' : 'Игра не запущена'));
      this.fpsMsg = gameName && !running ? 'Для этой игры нет данных FPS' : null;
      text('#fpsCurrent', running ? Math.round(f.current) : '—');
      text('#fpsAvg', running && isNum(f.avg) ? Math.round(f.avg) : '—');
      text('#fpsLow', running && isNum(f.low1) ? Math.round(f.low1) : '—');
      text('#fpsFrametime', running && f.current > 0 ? (1000 / f.current).toFixed(1).replace('.', ',') : '—');
      text('#overlayHud', running ? `${Math.round(f.current)} FPS` : '— FPS');
      if (running) this.push(this.hist.fps, f.current, FPS_LEN);
      else this.hist.fps.length = 0;

      const dot = $('.live-pill__dot');
      dot.classList.remove('is-tick');
      void dot.offsetWidth;
      dot.classList.add('is-tick');

      if (Nav.current === 'dashboard') this.drawCharts();
    },

    drawCharts() {
      drawSpark($('#cpuSpark'), this.hist.cpu, 100, '#2F80FF', '#6C8CFF');
      drawSpark($('#gpuSpark'), this.hist.gpu, 100, '#8B5CF6', '#B08BFF');
      drawSpark($('#ramSpark'), this.hist.ram, 100, '#1ED8A0', '#3FB4FF');
      drawFps($('#fpsGraph'), this.hist.fps, this.fpsMsg);
    },

    async toggleOverlay(input) {
      const enabled = input.checked;
      input.disabled = true;
      const res = await Bridge.call('toggle_fps_overlay', enabled);
      input.disabled = false;
      if (res.ok === false) {
        input.checked = !enabled;
        toast(errText(res, 'Не удалось переключить оверлей'), 'err');
        return;
      }
      store.set('overlay', enabled);
      $('#overlayMini').classList.toggle('is-on', enabled);
      toast(enabled ? 'Оверлей включён' : 'Оверлей выключен', enabled ? 'ok' : 'info', 2200);
    },
  };

  const OverlaySettings = {
    keys: ['W', 'A', 'S', 'D', 'Q', 'E', 'R', 'F', '1', '2', '3', '4', 'Shift', 'Ctrl', 'Space', 'Tab', 'Esc', 'Up', 'Down', 'Left', 'Right'],
    init() {
      const box = $('#overlayKeys');
      for (const key of this.keys) {
        const label = document.createElement('label');
        const input = document.createElement('input');
        input.type = 'checkbox'; input.value = key;
        label.append(input, document.createTextNode(key));
        box.append(label);
      }
      $('#overlaySave').addEventListener('click', () => this.save());
      $('#overlayChooseImage').addEventListener('click', () => this.chooseImage());
      $('#overlayClearImage').addEventListener('click', () => this.clearImage());
    },
    async load() {
      const res = await Bridge.call('get_overlay_settings');
      if (res.ok === false) return;
      $('#overlayPosition').value = res.position || 'top_left';
      $('#overlayOffsetX').value = res.offset_x ?? 16;
      $('#overlayOffsetY').value = res.offset_y ?? 16;
      $('#overlayDpi').value = res.dpi ?? 800;
      $('#overlayNickname').value = res.nickname || 'Player';
      const selected = new Set(res.keys || []);
      $$('#overlayKeys input').forEach((input) => { input.checked = selected.has(input.value); });
      this.showImage(res.image_path);
    },
    showImage(path) {
      $('#overlayImageName').textContent = path ? `Фото: ${String(path).split(/[\\/]/).pop()}` : 'Изображение не выбрано';
    },
    async save() {
      const settings = {
        position: $('#overlayPosition').value,
        offset_x: Number($('#overlayOffsetX').value), offset_y: Number($('#overlayOffsetY').value),
        dpi: Number($('#overlayDpi').value), nickname: $('#overlayNickname').value.trim(),
        keys: $$('#overlayKeys input:checked').map((input) => input.value),
      };
      const button = $('#overlaySave'); setLoading(button, true);
      const res = await Bridge.call('set_overlay_settings', settings);
      setLoading(button, false);
      if (res.ok === false) { toast(errText(res, 'Не удалось сохранить оверлей'), 'err'); return; }
      toast('Настройки оверлея сохранены', 'ok');
    },
    async chooseImage() {
      const res = await Bridge.call('choose_overlay_image');
      if (res.ok === false) { toast(errText(res, 'Не удалось выбрать изображение'), 'err'); return; }
      if (!res.cancelled) { this.showImage(res.image_path); toast('Изображение добавлено', 'ok'); }
    },
    async clearImage() {
      const res = await Bridge.call('clear_overlay_image');
      if (res.ok === false) { toast(errText(res, 'Не удалось убрать изображение'), 'err'); return; }
      this.showImage(null); toast('Изображение убрано', 'info');
    },
  };

  /* ---------------- Turbo Boost ---------------- */
  const Turbo = {
    busy: false,
    setStep(name, state) {
      const li = $(`#turboSteps li[data-step="${name}"]`);
      li.classList.remove('is-active', 'is-done');
      if (state) li.classList.add(state);
    },
    async run() {
      if (this.busy) return;
      this.busy = true;
      const btn = $('#turboBtn');
      clearTimeout(this.resetTimer);
      btn.classList.remove('is-done');
      btn.classList.add('is-running');
      btn.setAttribute('aria-busy', 'true');
      $('#turboLabel').textContent = 'Ускоряю…';
      btn.style.setProperty('--p', 0);
      this.setStep('ram', null);
      this.setStep('prio', null);

      void btn.offsetWidth;
      this.setStep('ram', 'is-active');
      btn.style.setProperty('--p', 55);

      const pending = Bridge.call('turbo_boost');
      await sleep(750);
      this.setStep('ram', 'is-done');
      this.setStep('prio', 'is-active');
      btn.style.setProperty('--p', 82);

      const res = await pending;
      btn.classList.remove('is-running');
      btn.setAttribute('aria-busy', 'false');

      if (res.ok === false) {
        this.setStep('ram', null);
        this.setStep('prio', null);
        btn.style.setProperty('--p', 0);
        $('#turboLabel').textContent = 'Ускорить';
        $('#turboResult').textContent = 'Буст не выполнен';
        toast(errText(res, 'Turbo Boost не выполнен'), 'err');
        this.busy = false;
        return;
      }

      this.setStep('prio', 'is-done');
      btn.style.setProperty('--p', 100);
      btn.classList.add('is-done');
      $('#turboLabel').textContent = 'Готово';

      const freed = fmtSize(res.freed_mb);
      const prio = res.priority_process
        ? `высокий приоритет у <b>${esc(res.priority_process)}</b>`
        : 'игра не запущена, приоритет не менялся';
      $('#turboResult').innerHTML = `Освобождено <b>${esc(freed)}</b>, ${prio}.`;
      toast(`Turbo Boost: освобождено ${freed}`, 'ok');

      this.resetTimer = setTimeout(() => {
        btn.classList.remove('is-done');
        btn.style.setProperty('--p', 0);
        $('#turboLabel').textContent = 'Ускорить';
        this.setStep('ram', null);
        this.setStep('prio', null);
      }, 3200);
      this.busy = false;
    },
  };

  /* ---------------- Цикл обновления статистики ---------------- */
  const Stats = {
    async loop() {
      let s;
      try { s = await Bridge.call('get_system_stats'); } catch (e) { s = null; }
      if (s && s.ok !== false) {
        Dash.render(s);
        Res.onStats(s);
      }
      setTimeout(() => this.loop(), 1000);
    },
  };

  /* =========================================================
     Игры и графика
     ========================================================= */
  const GAMES = [
    {
      id: 'fortnite', name: 'Fortnite', studio: 'Epic Games',
      extra: { label: 'Режим рендеринга', esports: 'DirectX 11', potato: 'Производительность' },
      art: (u) => `
        <svg viewBox="0 0 300 400" preserveAspectRatio="xMidYMid slice">
          <defs>
            <linearGradient id="${u}a" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#23176A"/><stop offset=".55" stop-color="#5A3BD6"/><stop offset="1" stop-color="#3DB2FF"/></linearGradient>
            <radialGradient id="${u}b" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#C47CFF" stop-opacity=".85"/><stop offset="1" stop-color="#C47CFF" stop-opacity="0"/></radialGradient>
          </defs>
          <rect width="300" height="400" fill="url(#${u}a)"/>
          <circle cx="150" cy="150" r="130" fill="url(#${u}b)"/>
          <circle cx="150" cy="150" r="98" fill="none" stroke="#EBD2FF" stroke-width="3" stroke-dasharray="2 9" stroke-linecap="round" opacity=".75"/>
          <circle cx="150" cy="150" r="66" fill="none" stroke="#fff" stroke-width="1.5" opacity=".45"/>
          <g fill="#fff" opacity=".8"><ellipse cx="60" cy="70" rx="34" ry="9"/><ellipse cx="236" cy="104" rx="42" ry="10"/><ellipse cx="258" cy="96" rx="20" ry="9"/></g>
          <path d="M0 292 C60 262 110 278 150 264 S240 244 300 268 V400 H0Z" fill="#22A673"/>
          <path d="M0 326 C80 304 140 320 200 306 S270 300 300 310 V400 H0Z" fill="#167650"/>
          <rect x="196" y="236" width="26" height="30" fill="#F2B84B"/><path d="M190 238 L209 222 L228 238Z" fill="#E0505F"/>
          <path d="M0 360 H300 V400 H0Z" fill="#0C3B2C"/>
        </svg>`,
    },
    {
      id: 'cs2', name: 'Counter-Strike 2', studio: 'Valve',
      render: { esports: 'Нативное', potato: 'Нативное' },
      extra: { label: 'Сглаживание', esports: '2x MSAA', potato: 'Выкл.' },
      art: (u) => `
        <svg viewBox="0 0 300 400" preserveAspectRatio="xMidYMid slice">
          <defs>
            <linearGradient id="${u}a" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#F6C27A"/><stop offset=".6" stop-color="#D9813D"/><stop offset="1" stop-color="#8E4A22"/></linearGradient>
            <linearGradient id="${u}b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0E2A33" stop-opacity="0"/><stop offset="1" stop-color="#0E2A33" stop-opacity=".85"/></linearGradient>
          </defs>
          <rect width="300" height="400" fill="url(#${u}a)"/>
          <circle cx="222" cy="96" r="38" fill="#FFE6B0" opacity=".9"/>
          <path d="M0 160 H104 V400 H0Z" fill="#B97E46"/>
          <path d="M0 160 H104 V172 H0Z" fill="#D59A5E"/>
          <path d="M112 134 H300 V400 H112Z" fill="#CC9154"/>
          <path d="M112 134 H300 V146 H112Z" fill="#E2AC6E"/>
          <path d="M150 400 V246 a56 56 0 0 1 112 0 V400Z" fill="#3A2416"/>
          <path d="M150 400 V246 a56 56 0 0 1 8 -29 V400Z" fill="#2A190F"/>
          <rect x="22" y="292" width="62" height="60" fill="#7A5230" stroke="#5A3A1F" stroke-width="3"/>
          <path d="M22 292 L84 352 M84 292 L22 352" stroke="#5A3A1F" stroke-width="3"/>
          <rect width="300" height="400" fill="url(#${u}b)"/>
        </svg>`,
    },
    {
      id: 'rust', name: 'Rust', studio: 'Facepunch Studios',
      render: { esports: 'Нативное', potato: 'Нативное' },
      extra: { label: 'Трава и растительность', esports: 'Низкая', potato: 'Выкл.' },
      art: (u) => `
        <svg viewBox="0 0 300 400" preserveAspectRatio="xMidYMid slice">
          <defs>
            <linearGradient id="${u}a" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1E1411"/><stop offset=".55" stop-color="#7A321A"/><stop offset="1" stop-color="#CE5A26"/></linearGradient>
            <pattern id="${u}p" width="6" height="6" patternUnits="userSpaceOnUse"><path d="M0 6 L6 0" stroke="#000" stroke-opacity=".12"/></pattern>
          </defs>
          <rect width="300" height="400" fill="url(#${u}a)"/>
          <circle cx="104" cy="250" r="54" fill="#FF8A3D" opacity=".85"/>
          <path d="M0 268 L60 250 L120 262 L190 240 L250 256 L300 246 V400 H0Z" fill="#3A1C10"/>
          <g stroke="#120A07" stroke-width="3.2" fill="none" stroke-linecap="round">
            <path d="M200 110 L170 330 M200 110 L230 330"/>
            <path d="M188 190 H212 M181 240 H219 M175 288 H225"/>
            <path d="M188 190 L219 240 M212 190 L181 240 M181 240 L225 288 M219 240 L175 288"/>
            <path d="M200 110 V84"/>
          </g>
          <circle cx="200" cy="82" r="4" fill="#FF3B30"/>
          <path d="M0 312 C70 300 140 318 210 304 S280 300 300 306 V400 H0Z" fill="#1C0F0A"/>
          <rect width="300" height="400" fill="url(#${u}p)"/>
        </svg>`,
    },
    {
      id: 'valorant', name: 'Valorant', studio: 'Riot Games',
      render: { esports: 'Нативное', potato: 'Нативное' },
      extra: { label: 'Детализация материалов', esports: 'Средняя', potato: 'Низкая' },
      art: (u) => `
        <svg viewBox="0 0 300 400" preserveAspectRatio="xMidYMid slice">
          <defs>
            <linearGradient id="${u}a" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1A2733"/><stop offset="1" stop-color="#0B121A"/></linearGradient>
          </defs>
          <rect width="300" height="400" fill="url(#${u}a)"/>
          <polygon points="300,0 300,230 130,0" fill="#FF4655"/>
          <polygon points="300,230 300,300 210,170" fill="#C8303F"/>
          <polygon points="0,400 0,262 150,400" fill="#FF4655" opacity=".9"/>
          <polygon points="60,120 120,120 70,260 10,260" fill="#ECE8E1" opacity=".07"/>
          <g stroke="#ECE8E1" stroke-opacity=".35" stroke-width="1.5">
            <path d="M20 60 H120"/><path d="M20 70 H70"/><path d="M180 350 H290"/><path d="M230 360 H290"/>
          </g>
          <rect x="24" y="300" width="8" height="8" fill="#FF4655"/>
          <rect x="38" y="300" width="8" height="8" fill="#ECE8E1" opacity=".5"/>
        </svg>`,
    },
  ];

  const PRESETS = {
    esports: { title: 'Киберспорт', render: '100%', textures: 'Средние', lod: 'Средняя', effects: 'Низкие', opts: { shadows: true, lod: false, latency: true } },
    potato: { title: 'Графика картошки', render: '75%', textures: 'Минимум', lod: 'Минимум', effects: 'Выкл.', opts: { shadows: true, lod: true, latency: true } },
    default: { title: 'Восстановить из .bak', opts: { shadows: false, lod: false, latency: false } },
  };

  const CUT_VALUES = new Set(['Минимум', 'Выкл.', 'Низкие', 'Низкая', 'Низкое', '75%', 'Производительность']);

  function buildChanges(game, preset, o) {
    if (preset === 'default') {
      return [['Все параметры графики', 'Восстановить из .bak']];
    }
    const P = PRESETS[preset];
    return [
      ['Разрешение рендера', (game.render && game.render[preset]) || P.render],
      ['Текстуры', o.lod ? 'Минимум' : P.textures],
      ['Детализация (LOD)', o.lod ? 'Минимум' : P.lod],
      ['Тени', o.shadows ? 'Выкл.' : 'Низкие'],
      ['Эффекты и постобработка', P.effects],
      [game.extra.label, game.extra[preset]],
      ['Низкая задержка', o.latency ? 'Вкл.' : 'Выкл.'],
    ];
  }

  const Games = {
    found: store.get('games.found', {}),
    profiles: store.get('games.profiles', {}),
    uid: 0,

    init() {
      this.render();
      $('#gamesGrid').addEventListener('click', (e) => {
        const card = e.target.closest('.game');
        if (card) Drawer.open(card.dataset.game, card);
      });
      $('#rescanBtn').addEventListener('click', () => this.scan(true));
      this.scan(false);
    },

    statusOf(id) {
      const f = this.found[id];
      if (!f || f.found === null || f.found === undefined) return { cls: 'status--unknown', label: 'Не проверена' };
      return f.found ? { cls: 'status--ok', label: 'Найдена' } : { cls: 'status--off', label: 'Не найдена' };
    },

    render() {
      $('#gamesGrid').innerHTML = GAMES.map((g) => {
        const st = this.statusOf(g.id);
        const prof = this.profiles[g.id];
        const missing = this.found[g.id] && this.found[g.id].found === false;
        const profLabel = prof ? `Профиль: ${PRESETS[prof.preset].title}` : 'Профиль не применялся';
        return `
          <button class="game${missing ? ' is-missing' : ''}" type="button" data-game="${g.id}" aria-label="${esc(g.name)}: ${st.label.toLowerCase()}. Открыть настройки графики">
            <span class="game__art" aria-hidden="true">${g.art('g' + (++this.uid))}</span>
            <span class="status ${st.cls}">${st.label}</span>
            <span class="game__info">
              <span class="game__studio">${esc(g.studio)}</span>
              <span class="game__title">${esc(g.name)}</span>
              <span class="game__profile${prof ? ' is-set' : ''}">${esc(profLabel)}</span>
            </span>
          </button>`;
      }).join('');
    },

    async scan(manual) {
      const btn = $('#rescanBtn');
      if (manual) setLoading(btn, true);
      const res = await Bridge.call('get_installed_games');
      if (manual) setLoading(btn, false);

      if (res.ok === false) {
        if (manual) toast(errText(res, 'Не удалось проверить установленные игры'), res.unsupported ? 'info' : 'err');
        return;
      }
      GAMES.forEach((g) => {
        const v = res[g.id];
        this.found[g.id] = v && typeof v === 'object' ? { found: !!v.found, path: v.path || null } : { found: !!v, path: null };
      });
      store.set('games.found', this.found);
      this.render();
      if (manual) {
        const n = GAMES.filter((g) => this.found[g.id].found).length;
        toast(`Найдено игр: ${n} из ${GAMES.length}`, 'ok');
      }
    },
  };

  /* ---------------- Панель игры ---------------- */
  const Drawer = {
    game: null,
    opener: null,

    init() {
      $('#drawerClose').addEventListener('click', () => this.close());
      $('#scrim').addEventListener('click', () => this.close());
      $$('input[name="preset"]').forEach((r) => r.addEventListener('change', () => this.onPreset(r.value)));
      $$('[data-opt]').forEach((t) => t.addEventListener('change', () => this.renderChanges()));
      $('#applyPresetBtn').addEventListener('click', () => this.apply());
      $('#backupBtn').addEventListener('click', () => this.backup());
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && $('#drawer').classList.contains('is-open')) this.close();
        if (e.key === 'Tab' && $('#drawer').classList.contains('is-open')) this.trapFocus(e);
      });
    },

    trapFocus(e) {
      const f = $$('#drawer button:not(:disabled), #drawer input:not(:disabled)').filter((el) => el.offsetParent !== null);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    },

    opts() {
      const o = {};
      $$('[data-opt]').forEach((t) => { o[t.dataset.opt] = t.checked; });
      return o;
    },

    preset() {
      const r = $('input[name="preset"]:checked');
      return r ? r.value : 'esports';
    },

    open(id, opener) {
      const g = GAMES.find((x) => x.id === id);
      if (!g) return;
      this.game = g;
      this.opener = opener || null;
      const st = Games.statusOf(id);
      const info = Games.found[id] || {};
      const missing = info.found === false;
      const prof = Games.profiles[id];
      const preset = prof ? prof.preset : 'esports';

      $('#drawerBanner').innerHTML = g.art('d' + (++Games.uid));
      $('#drawerTitle').textContent = g.name;
      const s = $('#drawerStatus');
      s.className = `status ${st.cls}`;
      s.textContent = st.label;
      $('#drawerPath').textContent = info.path || (missing ? '' : g.studio);
      $('#drawerNotice').hidden = !missing;
      $('#drawer').classList.toggle('is-missing', missing);
      $('#applyPresetBtn').disabled = missing;
      $('#backupBtn').disabled = missing;

      $$('input[name="preset"]').forEach((r) => { r.checked = r.value === preset; });
      const o = prof && prof.options ? prof.options : PRESETS[preset].opts;
      $$('[data-opt]').forEach((t) => { t.checked = !!o[t.dataset.opt]; });
      this.renderChanges();

      const d = $('#drawer');
      d.setAttribute('aria-hidden', 'false');
      d.classList.add('is-open');
      $('#scrim').classList.add('is-open');
      setTimeout(() => $('#drawerClose').focus(), 60);
    },

    close() {
      const d = $('#drawer');
      if (!d.classList.contains('is-open')) return;
      d.classList.remove('is-open');
      d.setAttribute('aria-hidden', 'true');
      $('#scrim').classList.remove('is-open');
      const sel = this.game ? `.game[data-game="${this.game.id}"]` : null;
      const back = (sel && $(sel)) || this.opener;
      if (back) back.focus({ preventScroll: true });
    },

    onPreset(p) {
      const o = PRESETS[p].opts;
      $$('[data-opt]').forEach((t) => { t.checked = !!o[t.dataset.opt]; });
      this.renderChanges();
    },

    renderChanges() {
      if (!this.game) return;
      const rows = buildChanges(this.game, this.preset(), this.opts());
      $('#presetChanges').innerHTML = rows.map(([k, v], i) =>
        `<div style="animation-delay:${i * 25}ms"><dt>${esc(k)}</dt><dd class="${CUT_VALUES.has(v) ? 'is-cut' : ''}">${esc(v)}</dd></div>`
      ).join('');
    },

    async apply() {
      const g = this.game;
      if (!g) return;
      const btn = $('#applyPresetBtn');
      const preset = this.preset();
      const options = this.opts();
      setLoading(btn, true);
      const res = await Bridge.call('apply_game_preset', g.id, preset);
      let optRes = { ok: true };
      if (res.ok !== false && preset !== 'default') {
        optRes = await Bridge.call('apply_game_options', g.id, options);
        if (optRes.unsupported) optRes = { ok: true };
      }
      setLoading(btn, false);

      if (res.ok === false) { toast(errText(res, `Не удалось применить конфиг к ${g.name}`), 'err'); return; }
      if (optRes.ok === false) toast(errText(optRes, 'Тонкие настройки не применились'), 'err');

      Games.profiles[g.id] = { preset, options, at: Date.now() };
      store.set('games.profiles', Games.profiles);
      Games.render();
      toast(res.message || `${g.name}: применён пресет «${PRESETS[preset].title}»`, 'ok');
    },

    async backup() {
      const g = this.game;
      if (!g) return;
      const btn = $('#backupBtn');
      setLoading(btn, true);
      const res = await Bridge.call('backup_game_config', g.id);
      setLoading(btn, false);
      if (res.ok === false) { toast(errText(res, 'Бэкап не создан'), 'err'); return; }
      toast(res.path ? `Бэкап сохранён: ${res.path}` : `Бэкап конфига ${g.name} сохранён`, 'ok', 4200);
    },
  };

  /* =========================================================
     Насыщенность
     ========================================================= */
  const Vib = {
    level: clamp(store.get('vib.level', 50), 50, 100),
    applied: clamp(store.get('vib.applied', 50), 50, 100),
    auto: !!store.get('vib.auto', false),
    split: clamp(store.get('vib.split', 50), 4, 96),
    timer: null,

    init() {
      const tpl = $('#tplVibScene');
      $('#compareBase').appendChild(tpl.content.cloneNode(true));
      $('#compareAfter').appendChild(tpl.content.cloneNode(true));

      const slider = $('#vibSlider');
      slider.value = this.level;
      slider.addEventListener('input', () => this.preview(+slider.value));
      slider.addEventListener('change', () => this.commit(+slider.value));

      $$('#vibChips .chip').forEach((c) => c.addEventListener('click', () => {
        const v = +c.dataset.level;
        slider.value = v;
        this.preview(v);
        this.commit(v, true);
      }));

      const auto = $('#vibAuto');
      auto.checked = this.auto;
      auto.addEventListener('change', () => this.setAuto(auto.checked));

      this.initCompare();
      this.preview(this.level);
      this.renderStatus();
    },

    preview(v) {
      this.level = v;
      const fill = ((v - 50) / 50) * 100;
      $('#vibSlider').style.setProperty('--fill', `${fill}%`);
      $('#vibValue').textContent = v;
      $('#vibValue').style.setProperty('--glow', (v - 50) / 50);
      $('#compare').style.setProperty('--sat', (1 + ((v - 50) / 50) * 1.1).toFixed(3));
      $('#tagAfter').textContent = `Ваша настройка, ${v}%`;
      $$('#vibChips .chip').forEach((c) => c.classList.toggle('is-active', +c.dataset.level === v));
      $('#vibSlider').setAttribute('aria-valuetext', `${v}%`);
    },

    commit(v, fromPreset = false) {
      store.set('vib.level', v);
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.send(v, fromPreset), 120);
    },

    async send(v, fromPreset) {
      const st = $('#vibStatus');
      st.classList.remove('is-error');
      st.classList.add('is-pending');
      $('#vibStatusText').textContent = this.auto ? 'Сохраняю настройку…' : `Применяю ${v}%…`;

      let res;
      if (this.auto) {
        res = await Bridge.call('set_vibrance_auto', true, v);
      } else {
        res = await Bridge.call('apply_vibrance', v);
        if (res.ok !== false) { this.applied = v; store.set('vib.applied', v); }
      }
      if (v !== this.level) return; // пользователь уже двигает ползунок дальше

      st.classList.remove('is-pending');
      if (res.ok === false) {
        st.classList.add('is-error');
        $('#vibStatusText').textContent = errText(res, 'Не удалось изменить насыщенность');
        toast(errText(res, 'Не удалось изменить насыщенность'), 'err');
        return;
      }
      this.renderStatus();
      if (fromPreset) toast(this.auto ? `Насыщенность ${v}% включится при запуске игры` : `Насыщенность ${v}% применена`, 'ok', 2400);
    },

    async setAuto(on) {
      const input = $('#vibAuto');
      input.disabled = true;
      const res = await Bridge.call('set_vibrance_auto', on, this.level);
      input.disabled = false;
      if (res.ok === false && !res.unsupported) {
        input.checked = !on;
        toast(errText(res, 'Не удалось сохранить режим'), 'err');
        return;
      }
      if (res.unsupported) toast('Бэкенд не умеет применять насыщенность при запуске игры: нужен метод set_vibrance_auto()', 'info', 4200);
      this.auto = on;
      store.set('vib.auto', on);
      if (!on) {
        // режим «только в игре» выключен — применяем на рабочий стол сразу
        await this.send(this.level, false);
      } else {
        // вне игры возвращаем стандарт, насыщенность включится при запуске
        if (this.applied !== 50) {
          const r = await Bridge.call('apply_vibrance', 50);
          if (r.ok !== false) { this.applied = 50; store.set('vib.applied', 50); }
        }
        this.renderStatus();
        toast(`Насыщенность ${this.level}% будет включаться при запуске игры`, 'ok', 2600);
      }
    },

    renderStatus() {
      const st = $('#vibStatus');
      st.classList.remove('is-pending', 'is-error');
      $('#vibStatusText').innerHTML = this.auto
        ? `Включится при запуске игры: <b class="mono">${this.level}%</b>`
        : `Сейчас на мониторе: <b class="mono">${this.applied}%</b>`;
    },

    initCompare() {
      const box = $('#compare');
      const handle = $('#compareHandle');
      const set = (pct) => {
        this.split = clamp(pct, 4, 96);
        box.style.setProperty('--split', `${this.split}%`);
        handle.setAttribute('aria-valuenow', Math.round(this.split));
      };
      set(this.split);
      const fromEvent = (e) => {
        const r = box.getBoundingClientRect();
        set(((e.clientX - r.left) / r.width) * 100);
      };
      box.addEventListener('pointerdown', (e) => {
        box.setPointerCapture(e.pointerId);
        box.classList.add('is-dragging');
        fromEvent(e);
      });
      box.addEventListener('pointermove', (e) => { if (box.hasPointerCapture(e.pointerId)) fromEvent(e); });
      const end = (e) => {
        if (box.hasPointerCapture(e.pointerId)) box.releasePointerCapture(e.pointerId);
        box.classList.remove('is-dragging');
        store.set('vib.split', this.split);
      };
      box.addEventListener('pointerup', end);
      box.addEventListener('pointercancel', end);
      handle.addEventListener('keydown', (e) => {
        const d = { ArrowLeft: -5, ArrowRight: 5, Home: -100, End: 100 }[e.key];
        if (d === undefined) return;
        e.preventDefault();
        set(this.split + d);
        store.set('vib.split', this.split);
      });
    },
  };

  /* =========================================================
     Растяг экрана
     ========================================================= */
  const RES_PRESETS = [
    { w: 1920, h: 1080, ratio: '16:9', note: 'Без растяга' },
    { w: 1440, h: 1080, ratio: '4:3', note: 'Классика CS' },
    { w: 1280, h: 960, ratio: '4:3', note: 'Больше FPS' },
    { w: 1720, h: 1080, ratio: '≈16:10', note: 'Лёгкий растяг' },
    { w: 1600, h: 1080, ratio: '≈3:2', note: 'Средний растяг' },
  ];

  const LIMITS = { w: [640, 7680], h: [480, 4320], hz: [30, 540] };
  const inRange = (v, [a, b]) => isNum(v) && v >= a && v <= b;
  const SAVED_RES = store.get('res.state', null);

  const Res = {
    native: { width: 1920, height: 1080, hz: 240 },
    nativeKnown: false,
    state: Object.assign({ w: 1440, h: 1080, hz: 240 }, SAVED_RES || {}),
    stretch: store.get('res.stretch', true),
    timer: null,

    init() {
      $('#gameFrame').appendChild($('#tplResScene').content.cloneNode(true));
      this.renderTiles();

      const W = $('#resW'), H = $('#resH'), HZ = $('#resHz');
      W.value = this.state.w; H.value = this.state.h; HZ.value = this.state.hz;
      [W, H, HZ].forEach((inp) => inp.addEventListener('input', () => {
        this.state = { w: parseInt(W.value, 10), h: parseInt(H.value, 10), hz: parseInt(HZ.value, 10) };
        this.validate();
        this.update();
      }));
      [W, H, HZ].forEach((inp) => inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') this.apply(); }));

      const st = $('#stretchToggle');
      st.checked = this.stretch;
      st.addEventListener('change', async () => {
        this.stretch = st.checked;
        store.set('res.stretch', this.stretch);
        this.update();
        const r = await Bridge.call('set_scaling_mode', this.stretch);
        if (r.ok === false && !r.unsupported) toast(errText(r, 'Не удалось изменить масштабирование'), 'err');
      });

      $('#applyResBtn').addEventListener('click', () => this.apply());
      $('#resetResBtn').addEventListener('click', () => this.reset());
      $('#resKeep').addEventListener('click', () => this.confirm(true));
      $('#resRevert').addEventListener('click', () => this.confirm(false));
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && $('#resModal').classList.contains('is-open')) this.confirm(false);
      });

      this.validate();
      this.update();
    },

    onStats(s) {
      const d = s && s.display;
      if (!d || !isNum(d.width) || !isNum(d.height)) return;
      // родное разрешение запоминаем один раз — до любых изменений
      if (this.nativeKnown) return;
      this.native = { width: d.width, height: d.height, hz: isNum(d.hz) ? d.hz : this.native.hz };
      this.nativeKnown = true;
      if (!SAVED_RES) {
        this.state.hz = this.native.hz;
        $('#resHz').value = this.native.hz;
      }
      RES_PRESETS[0].w = this.native.width;
      RES_PRESETS[0].h = this.native.height;
      RES_PRESETS[0].ratio = this.ratioLabel(this.native.width, this.native.height);
      this.renderTiles();
      this.update();
    },

    ratioLabel(w, h) {
      const g = (a, b) => (b ? g(b, a % b) : a);
      const d = g(w, h);
      const rw = w / d, rh = h / d;
      return rw <= 32 && rh <= 32 ? `${rw}:${rh}` : `${(w / h).toFixed(2)}:1`;
    },

    renderTiles() {
      const tiles = RES_PRESETS.map((p, i) => `
          <button class="res-tile" type="button" role="radio" aria-checked="false" data-w="${p.w}" data-h="${p.h}">
            <span class="res-tile__tag">${esc(p.ratio)}</span>
            <span class="res-tile__shape" aria-hidden="true"><i style="--ar:${p.w}/${p.h}"></i></span>
            <b>${p.w}×${p.h}</b>
            <small>${i === 0 ? 'Родное' : esc(p.note)}</small>
          </button>`).join('') + `
          <button class="res-tile res-tile--custom" type="button" role="radio" aria-checked="false" data-custom="1">
            <span class="res-tile__shape" aria-hidden="true"><i style="--ar:5/4"></i></span>
            <b>Своё</b>
            <small>Введите ниже</small>
          </button>`;
      const grid = $('#resGrid');
      grid.innerHTML = tiles;
      $$('.res-tile', grid).forEach((t) => t.addEventListener('click', () => {
        if (t.dataset.custom) { $('#resW').focus(); $('#resW').select(); return; }
        this.state.w = +t.dataset.w;
        this.state.h = +t.dataset.h;
        $('#resW').value = this.state.w;
        $('#resH').value = this.state.h;
        this.validate();
        this.update();
      }));
    },

    validate() {
      const { w, h, hz } = this.state;
      const bad = [];
      const chk = (id, v, [a, b], name) => {
        const ok = inRange(v, [a, b]);
        $(id).classList.toggle('is-invalid', !ok);
        if (!ok) bad.push(`${name}: от ${a} до ${b}`);
      };
      chk('#resW', w, LIMITS.w, 'ширина');
      chk('#resH', h, LIMITS.h, 'высота');
      chk('#resHz', hz, LIMITS.hz, 'частота');
      const err = $('#resError');
      err.hidden = !bad.length;
      err.textContent = bad.length ? `Проверьте значения — ${bad.join(', ')}.` : '';
      $('#applyResBtn').disabled = !!bad.length;
      return !bad.length;
    },

    update() {
      const { w, h } = this.state;
      const valid = inRange(w, LIMITS.w) && inRange(h, LIMITS.h);

      // плитки
      let matched = false;
      $$('.res-tile').forEach((t) => {
        const on = !t.dataset.custom && valid && +t.dataset.w === w && +t.dataset.h === h;
        if (on) matched = true;
        t.classList.toggle('is-active', on);
        t.setAttribute('aria-checked', on ? 'true' : 'false');
      });
      const custom = $('.res-tile--custom');
      if (custom) {
        custom.classList.toggle('is-active', valid && !matched);
        custom.setAttribute('aria-checked', valid && !matched ? 'true' : 'false');
        if (valid && !matched) {
          custom.querySelector('b').textContent = `${w}×${h}`;
          custom.querySelector('i').style.setProperty('--ar', `${w}/${h}`);
        } else {
          custom.querySelector('b').textContent = 'Своё';
          custom.querySelector('i').style.setProperty('--ar', '5/4');
        }
      }

      const n = this.native;
      $('#resNative').textContent = `Родное: ${n.width}×${n.height}, ${n.hz} Гц`;
      $('#monScreen').style.setProperty('--native-ar', `${n.width} / ${n.height}`);

      if (!valid) { $('#resEffect').textContent = 'Введите разрешение в допустимых пределах'; return; }

      // превью растяга
      const nAR = n.width / n.height;
      const tAR = w / h;
      const frame = $('#gameFrame');
      let fw = 100, fh = 100;
      if (tAR < nAR) fw = (tAR / nAR) * 100; else fh = (nAR / tAR) * 100;
      frame.style.setProperty('--fw', `${fw}%`);
      frame.style.setProperty('--fh', `${fh}%`);
      frame.style.setProperty('--sx', this.stretch ? 100 / fw : 1);
      frame.style.setProperty('--sy', this.stretch ? 100 / fh : 1);

      const eff = $('#resEffect');
      if (Math.abs(tAR - nAR) < 0.01) {
        eff.textContent = 'Пропорции совпадают с монитором: растяга нет';
      } else if (tAR < nAR) {
        const pct = Math.round((nAR / tAR - 1) * 100);
        eff.innerHTML = this.stretch
          ? `Модели и прицел станут шире на <b>${pct}%</b>`
          : 'Пропорции без изменений, чёрные полосы по бокам';
      } else {
        const pct = Math.round((tAR / nAR - 1) * 100);
        eff.innerHTML = this.stretch
          ? `Модели станут выше на <b>${pct}%</b>`
          : 'Пропорции без изменений, чёрные полосы сверху и снизу';
      }
      store.set('res.state', this.state);
    },

    async apply() {
      if (!this.validate()) return;
      const { w, h, hz } = this.state;
      const btn = $('#applyResBtn');
      setLoading(btn, true);
      const sc = await Bridge.call('set_scaling_mode', this.stretch);
      if (sc.ok === false && !sc.unsupported) toast(errText(sc, 'Масштабирование не изменено'), 'err');
      const res = await Bridge.call('apply_resolution', w, h, hz);
      setLoading(btn, false);
      if (res.ok === false) { toast(errText(res, `Разрешение ${w}×${h} не применилось`), 'err'); return; }
      this.pending = { w, h, hz };
      this.openConfirm();
    },

    openConfirm() {
      const m = $('#resModal');
      const { w, h, hz } = this.pending;
      $('#resModalTitle').textContent = `Оставить ${w}×${h}, ${hz} Гц?`;
      m.classList.add('is-open');
      m.setAttribute('aria-hidden', 'false');
      setTimeout(() => $('#resKeep').focus(), 50);

      const total = 15;
      let left = total;
      const bar = $('#resTimerBar');
      $('#resCountdown').textContent = left;
      bar.style.transition = 'none';
      bar.style.transform = 'scaleX(1)';
      void bar.offsetWidth;
      bar.style.transition = `transform ${total}s linear`;
      bar.style.transform = 'scaleX(0)';

      clearInterval(this.timer);
      this.timer = setInterval(() => {
        left -= 1;
        $('#resCountdown').textContent = Math.max(0, left);
        if (left <= 0) this.confirm(false, true);
      }, 1000);
    },

    async confirm(keep, auto = false) {
      clearInterval(this.timer);
      const m = $('#resModal');
      m.classList.remove('is-open');
      m.setAttribute('aria-hidden', 'true');
      if (keep) {
        const confirmation = await Bridge.call('confirm_resolution');
        if (confirmation.ok === false && !confirmation.unsupported) {
          toast(errText(confirmation, 'Не удалось подтвердить разрешение'), 'err');
          return;
        }
        const { w, h, hz } = this.pending || this.state;
        toast(`Разрешение ${w}×${h}, ${hz} Гц оставлено`, 'ok');
        $('#applyResBtn').focus();
        return;
      }
      await this.reset(auto ? 'Время вышло, родное разрешение возвращено' : null);
    },

    async reset(message = null) {
      const btn = $('#resetResBtn');
      setLoading(btn, true);
      const res = await Bridge.call('reset_resolution');
      setLoading(btn, false);
      if (res.ok === false) { toast(errText(res, 'Не удалось вернуть родное разрешение'), 'err'); return; }
      const n = this.native;
      toast(message || `Родное разрешение ${n.width}×${n.height} возвращено`, message ? 'info' : 'ok');
    },
  };

  /* =========================================================
     Окно
     ========================================================= */
  function initWindow() {
    $('#btnMinimize').addEventListener('click', () => Bridge.call('minimize_window'));
    $('#btnClose').addEventListener('click', () => Bridge.call('close_window'));
  }

  /* =========================================================
     Запуск
     ========================================================= */
  // После перезапуска программы возвращаем бэкенду сохранённые режимы
  async function restoreBackendState() {
    if (store.get('overlay', false)) {
      const r = await Bridge.call('toggle_fps_overlay', true);
      if (r.ok === false) { $('#overlayToggle').checked = false; $('#overlayMini').classList.remove('is-on'); store.set('overlay', false); }
    }
    if (Vib.auto) await Bridge.call('set_vibrance_auto', true, Vib.level);
    await Bridge.call('set_scaling_mode', Res.stretch);
  }

  async function boot() {
    Nav.init();
    initWindow();
    Dash.init();
    OverlaySettings.init();
    Drawer.init();
    Vib.init();
    Res.init();

    const live = await waitForBridge();
    setLive(live);
    await OverlaySettings.load();
    if (!live) {
      // в демо-режиме сразу заполняем графики историей, чтобы превью не было пустым
      for (let i = 0; i < FPS_LEN; i++) {
        const s = await Mock.get_system_stats();
        if (i >= FPS_LEN - SPARK_LEN) {
          Dash.push(Dash.hist.cpu, s.cpu.load, SPARK_LEN);
          Dash.push(Dash.hist.gpu, s.gpu.load, SPARK_LEN);
          Dash.push(Dash.hist.ram, (s.ram.used / s.ram.total) * 100, SPARK_LEN);
        }
        Dash.push(Dash.hist.fps, s.fps.current, FPS_LEN);
      }
      // pywebview может подняться позже — переключимся на живые данные
      window.addEventListener('pywebviewready', () => {
        setLive(true);
        OverlaySettings.load();
        restoreBackendState();
        Dash.hist = { cpu: [], gpu: [], ram: [], fps: [] };
        Res.nativeKnown = false;
        Games.scan(false);
        toast('Бэкенд подключён, показываю данные этого ПК', 'ok');
      }, { once: true });
    }

    if (live) restoreBackendState();
    Games.init();
    Stats.loop();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
