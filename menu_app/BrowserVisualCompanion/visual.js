(() => {
  if (window.top !== window || window.__macMcpVisualCompanionLoaded) return;
  window.__macMcpVisualCompanionLoaded = true;

  const EVENT_ATTR = 'data-mac-mcp-visual-event';
  const HOST_ID = 'mac-mcp-visual-companion-root';
  const HISTORY_KEY = '__mac_mcp_visual_history_v1';
  const MAX_HISTORY = 30;
  const SAFE_TARGETS = new Set(['Page', 'Button', 'Link', 'Text field', 'Menu', 'Checkbox', 'Option', 'Tab', 'Date', 'Item']);
  const SAFE_DETAILS = new Set(['Up', 'Down', 'Into view']);
  const HISTORY_LABELS = {
    Inspecting: 'Inspected page',
    Finding: 'Searched page',
    Reading: 'Read page',
    Clicking: 'Clicked',
    Typing: 'Typed',
    Selecting: 'Selected',
    Focusing: 'Focused',
    Scrolling: 'Scrolled',
    Opened: 'Opened page',
    Working: 'Worked'
  };
  const HISTORY_ICONS = {
    Inspecting: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2.7 10s2.7-4.5 7.3-4.5 7.3 4.5 7.3 4.5-2.7 4.5-7.3 4.5S2.7 10 2.7 10Z"/><circle cx="10" cy="10" r="2.2"/></svg>',
    Finding: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8.6" cy="8.6" r="4.9"/><path d="m12.3 12.3 4.1 4.1"/></svg>',
    Reading: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4.2h8.2a2 2 0 0 1 2 2v9.6H6a2 2 0 0 1-2-2V4.2Z"/><path d="M6.8 7.2h4.7M6.8 10h4.7M6.8 12.8h3"/></svg>',
    Clicking: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 3.2 7.5 7.1-4.1.7 2.3 4.3-2.1 1.1-2.2-4.2-2.8 3V4.1Z"/></svg>',
    Typing: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4.1 5.1h11.8M10 5.1v9.8M7.1 14.9h5.8"/></svg>',
    Selecting: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4.2 6.2h11.6l-5.8 7.6-5.8-7.6Z"/></svg>',
    Focusing: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="5.6"/><circle cx="10" cy="10" r="1.8"/></svg>',
    Scrolling: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 3v14M6.7 6.3 10 3l3.3 3.3M6.7 13.7 10 17l3.3-3.3"/></svg>',
    Opened: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4.5h12v11H4z"/><path d="M4 7.2h12"/></svg>',
    Working: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="2"/><circle cx="4" cy="10" r="1"/><circle cx="16" cy="10" r="1"/></svg>'
  };

  let hideTimer = null;
  let cursorTimer = null;
  let lastSeq = null;
  let history = loadHistory();

  function loadHistory() {
    try {
      const raw = sessionStorage.getItem(HISTORY_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.slice(-MAX_HISTORY).filter((item) => item && typeof item === 'object');
    } catch (_) { return []; }
  }

  function saveHistory() {
    try { sessionStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-MAX_HISTORY))); } catch (_) {}
  }

  function safeTarget(value) {
    const target = String(value || '');
    return SAFE_TARGETS.has(target) ? target : '';
  }

  function safeDetail(value) {
    const detail = String(value || '');
    return SAFE_DETAILS.has(detail) ? detail : '';
  }

  function historyLabel(action) {
    return HISTORY_LABELS[action] || 'Worked';
  }

  function formatClock(timestamp) {
    try {
      return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (_) { return ''; }
  }

  function ensureUI() {
    let host = document.getElementById(HOST_ID);
    if (host) return host.shadowRoot;
    host = document.createElement('div');
    host.id = HOST_ID;
    host.style.cssText = 'all:initial!important;position:fixed!important;inset:0!important;z-index:2147483647!important;pointer-events:none!important;contain:strict!important;';
    const shadow = host.attachShadow({ mode: 'open' });
    shadow.innerHTML = `
      <style>
        :host { all: initial; }
        * { box-sizing: border-box; }
        .companion { position: fixed; inset: 0; pointer-events: none; color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif; }
        .frame { position: fixed; inset: 2px; border: 2px solid rgba(90, 200, 250, .78); border-radius: 10px; box-shadow: inset 0 0 0 1px rgba(255,255,255,.16), 0 0 18px rgba(90,200,250,.28); opacity: 0; transition: opacity .18s ease, border-color .18s ease, box-shadow .18s ease; animation: none; }
        .frame.active { opacity: 1; animation: mcpPulse 1.7s ease-in-out infinite; }
        .frame.attention { border-color: rgba(255, 184, 74, .9); box-shadow: 0 0 20px rgba(255,184,74,.28); }
        .pill { position: fixed; top: 14px; right: 14px; display: flex; align-items: center; gap: 8px; min-height: 28px; padding: 0 10px; border: 1px solid rgba(255,255,255,.18); border-radius: 999px; background: rgba(17,20,25,.78); color: rgba(255,255,255,.94); box-shadow: 0 7px 24px rgba(0,0,0,.22); backdrop-filter: none; -webkit-backdrop-filter: none; font: 600 12px/1 -apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif; letter-spacing: -.01em; opacity: 0; transform: translateY(-5px) scale(.98); transition: opacity .16s ease, transform .16s ease, right .38s cubic-bezier(.22,.8,.2,1); }
        .pill.active { opacity: 1; transform: translateY(0) scale(1); backdrop-filter: blur(16px) saturate(140%); -webkit-backdrop-filter: blur(16px) saturate(140%); }
        .companion.sidebar-open .pill { right: 346px; }
        .dot { width: 7px; height: 7px; border-radius: 50%; background: rgb(90,200,250); box-shadow: 0 0 0 0 rgba(90,200,250,.45); animation: none; }
        .pill.active .dot { animation: mcpDot 1.35s ease-out infinite; }
        .label { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .cursor { position: fixed; left: 0; top: 0; width: 18px; height: 24px; opacity: 0; transform: translate3d(-40px,-40px,0); transition: transform .24s cubic-bezier(.22,.8,.2,1), opacity .12s ease; filter: drop-shadow(0 2px 4px rgba(0,0,0,.32)); }
        .cursor::before { content: ''; position: absolute; inset: 0; background: white; clip-path: polygon(0 0, 0 90%, 25% 69%, 40% 100%, 51% 94%, 36% 65%, 66% 64%); }
        .cursor::after { content: ''; position: absolute; inset: 1px; background: #111; clip-path: polygon(0 0, 0 90%, 25% 69%, 40% 100%, 51% 94%, 36% 65%, 66% 64%); z-index: -1; transform: scale(1.08); transform-origin: top left; }
        .cursor.active { opacity: .96; }
        .ripple { position: fixed; width: 12px; height: 12px; margin: -6px 0 0 -6px; border-radius: 50%; border: 2px solid rgba(90,200,250,.9); opacity: 0; transform: scale(.35); }
        .ripple.go { animation: mcpRipple .52s ease-out forwards; }

        .history-rail { position: fixed; right: 0; top: 50%; width: 17px; height: 82px; transform: translateY(-50%); pointer-events: auto; border: 1px solid rgba(255,255,255,.14); border-right: 0; border-radius: 13px 0 0 13px; background: rgba(18,21,27,.64); box-shadow: -5px 7px 20px rgba(0,0,0,.16); backdrop-filter: none; -webkit-backdrop-filter: none; opacity: .48; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: opacity .18s ease, width .18s ease, right .38s cubic-bezier(.22,.8,.2,1), background .18s ease; }
        .history-rail:hover { opacity: .94; width: 21px; background: rgba(18,21,27,.82); backdrop-filter: blur(18px) saturate(135%); -webkit-backdrop-filter: blur(18px) saturate(135%); }
        .history-rail::before { content: ''; width: 3px; height: 32px; border-radius: 999px; background: linear-gradient(180deg, rgba(90,200,250,.9), rgba(90,200,250,.24)); box-shadow: 0 0 13px rgba(90,200,250,.32); }
        .history-rail .rail-chevron { position: absolute; left: 3px; top: 50%; width: 7px; height: 7px; border-left: 1.5px solid rgba(255,255,255,.72); border-bottom: 1.5px solid rgba(255,255,255,.72); transform: translateY(-50%) rotate(45deg); opacity: 0; transition: opacity .18s ease; }
        .history-rail:hover .rail-chevron { opacity: .9; }
        .history-rail .rail-count { position: absolute; top: -6px; left: -15px; min-width: 21px; height: 21px; padding: 0 5px; border-radius: 999px; display: flex; align-items: center; justify-content: center; background: rgba(31,36,44,.94); border: 1px solid rgba(255,255,255,.14); color: rgba(255,255,255,.86); font-size: 10px; font-weight: 700; opacity: 0; transform: scale(.82); transition: opacity .18s ease, transform .18s ease; }
        .history-rail.has-history .rail-count { opacity: 1; transform: scale(1); }
        .companion.sidebar-open .history-rail { right: 330px; opacity: .96; width: 20px; }
        .companion.sidebar-open .history-rail .rail-chevron { opacity: .9; transform: translateY(-50%) rotate(225deg); }

        .history-panel { position: fixed; top: 12px; right: 12px; bottom: 12px; width: 318px; pointer-events: none; overflow: hidden; display: flex; flex-direction: column; border-radius: 22px; border: 1px solid rgba(255,255,255,.14); background: linear-gradient(180deg, rgba(24,28,35,.91), rgba(13,16,21,.90)); box-shadow: 0 24px 68px rgba(0,0,0,.34), inset 0 1px 0 rgba(255,255,255,.07); backdrop-filter: none; -webkit-backdrop-filter: none; visibility: hidden; opacity: 0; transform: translate3d(34px,0,0) scale(.985); transform-origin: right center; transition: opacity .23s ease, transform .38s cubic-bezier(.22,.8,.2,1); }
        .companion.sidebar-open .history-panel { pointer-events: auto; visibility: visible; opacity: 1; transform: translate3d(0,0,0) scale(1); backdrop-filter: blur(28px) saturate(135%); -webkit-backdrop-filter: blur(28px) saturate(135%); }
        .history-header { flex: 0 0 auto; padding: 18px 18px 14px; border-bottom: 1px solid rgba(255,255,255,.075); }
        .history-header-row { display: flex; align-items: center; gap: 10px; }
        .history-brand { width: 32px; height: 32px; border-radius: 10px; display: flex; align-items: center; justify-content: center; background: rgba(90,200,250,.11); border: 1px solid rgba(90,200,250,.18); }
        .history-brand svg { width: 18px; height: 18px; fill: none; stroke: rgb(116,211,255); stroke-width: 1.65; stroke-linecap: round; stroke-linejoin: round; }
        .history-titles { min-width: 0; flex: 1; }
        .history-title { color: rgba(255,255,255,.96); font-size: 13px; font-weight: 700; letter-spacing: -.015em; line-height: 1.1; }
        .history-subtitle { margin-top: 4px; color: rgba(255,255,255,.48); font-size: 11px; font-weight: 500; }
        .history-close { width: 27px; height: 27px; padding: 0; border: 0; border-radius: 9px; color: rgba(255,255,255,.52); background: rgba(255,255,255,.055); cursor: pointer; font-size: 17px; line-height: 27px; transition: color .16s ease, background .16s ease; }
        .history-close:hover { color: rgba(255,255,255,.92); background: rgba(255,255,255,.11); }
        .history-live { margin-top: 13px; display: flex; align-items: center; justify-content: space-between; gap: 10px; }
        .history-live-status { display: flex; align-items: center; gap: 7px; color: rgba(255,255,255,.62); font-size: 10.5px; font-weight: 600; }
        .history-live-dot { width: 6px; height: 6px; border-radius: 50%; background: rgba(255,255,255,.28); transition: background .18s ease, box-shadow .18s ease; }
        .history-live-status.active .history-live-dot { background: rgb(90,200,250); box-shadow: 0 0 0 4px rgba(90,200,250,.10); }
        .history-clear { padding: 5px 8px; border: 0; border-radius: 8px; background: transparent; color: rgba(255,255,255,.42); font-size: 10.5px; font-weight: 600; cursor: pointer; transition: color .16s ease, background .16s ease; }
        .history-clear:hover { color: rgba(255,255,255,.86); background: rgba(255,255,255,.06); }
        .history-list { flex: 1 1 auto; overflow-y: auto; padding: 10px 12px 14px; scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.12) transparent; }
        .history-empty { height: 100%; min-height: 180px; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 24px; color: rgba(255,255,255,.42); }
        .history-empty-icon { width: 40px; height: 40px; border-radius: 14px; margin-bottom: 12px; display: flex; align-items: center; justify-content: center; border: 1px solid rgba(255,255,255,.08); background: rgba(255,255,255,.035); }
        .history-empty-icon svg { width: 20px; height: 20px; fill: none; stroke: rgba(255,255,255,.42); stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
        .history-empty strong { color: rgba(255,255,255,.72); font-size: 12px; margin-bottom: 5px; }
        .history-empty span { max-width: 210px; font-size: 10.5px; line-height: 1.45; }
        .history-item { position: relative; display: grid; grid-template-columns: 34px minmax(0,1fr) auto; gap: 10px; align-items: start; padding: 9px 8px; border-radius: 13px; transition: background .14s ease; }
        .history-item:hover { background: rgba(255,255,255,.035); }
        .history-item + .history-item::before { content: ''; position: absolute; left: 24.5px; top: -8px; width: 1px; height: 10px; background: rgba(255,255,255,.08); }
        .history-icon { width: 32px; height: 32px; border-radius: 10px; display: flex; align-items: center; justify-content: center; background: rgba(255,255,255,.045); border: 1px solid rgba(255,255,255,.07); }
        .history-icon svg { width: 16px; height: 16px; fill: none; stroke: rgba(158,220,250,.9); stroke-width: 1.55; stroke-linecap: round; stroke-linejoin: round; }
        .history-copy { min-width: 0; padding-top: 2px; }
        .history-action { color: rgba(255,255,255,.88); font-size: 11.5px; font-weight: 650; letter-spacing: -.005em; line-height: 1.2; }
        .history-detail { margin-top: 4px; color: rgba(255,255,255,.42); font-size: 10.5px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .history-time { padding-top: 3px; color: rgba(255,255,255,.28); font-size: 9.5px; font-variant-numeric: tabular-nums; }
        .history-footer { flex: 0 0 auto; padding: 11px 16px 13px; border-top: 1px solid rgba(255,255,255,.065); display: flex; align-items: center; gap: 7px; color: rgba(255,255,255,.32); font-size: 9.5px; }
        .history-footer-dot { width: 4px; height: 4px; border-radius: 50%; background: rgba(90,200,250,.55); }

        @keyframes mcpPulse { 0%,100% { box-shadow: 0 0 12px rgba(90,200,250,.16); } 50% { box-shadow: 0 0 24px rgba(90,200,250,.38); } }
        @keyframes mcpDot { 0% { box-shadow: 0 0 0 0 rgba(90,200,250,.48); } 65%,100% { box-shadow: 0 0 0 6px rgba(90,200,250,0); } }
        @keyframes mcpRipple { 0% { opacity: .92; transform: scale(.35); } 100% { opacity: 0; transform: scale(2.8); } }
        @media (prefers-reduced-motion: reduce) { .frame,.dot { animation: none; } .cursor,.pill,.history-panel,.history-rail { transition-duration: .01ms; } }
      </style>
      <div class="companion">
        <div class="frame"></div>
        <div class="pill"><span class="dot"></span><span class="label">Mac MCP · Working</span></div>
        <div class="cursor"></div>
        <div class="ripple"></div>
        <button class="history-rail" type="button" aria-label="Open Mac MCP tab activity">
          <span class="rail-chevron"></span><span class="rail-count">0</span>
        </button>
        <aside class="history-panel" aria-label="Mac MCP tab activity">
          <div class="history-header">
            <div class="history-header-row">
              <div class="history-brand"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5.1 4.8h9.8v10.4H5.1z"/><path d="M7.7 8h4.6M7.7 11h4.6"/></svg></div>
              <div class="history-titles"><div class="history-title">Mac MCP</div><div class="history-subtitle">Tab activity</div></div>
              <button class="history-close" type="button" aria-label="Close activity panel">×</button>
            </div>
            <div class="history-live">
              <div class="history-live-status"><span class="history-live-dot"></span><span class="history-live-copy">Ready</span></div>
              <button class="history-clear" type="button">Clear</button>
            </div>
          </div>
          <div class="history-list"></div>
          <div class="history-footer"><span class="history-footer-dot"></span><span>This tab only · no page content stored</span></div>
        </aside>
      </div>`;
    (document.documentElement || document).appendChild(host);

    const companion = shadow.querySelector('.companion');
    const rail = shadow.querySelector('.history-rail');
    const close = shadow.querySelector('.history-close');
    const clear = shadow.querySelector('.history-clear');
    const stop = (event) => { event.preventDefault(); event.stopPropagation(); };
    rail.addEventListener('click', (event) => { stop(event); companion.classList.toggle('sidebar-open'); });
    close.addEventListener('click', (event) => { stop(event); companion.classList.remove('sidebar-open'); });
    clear.addEventListener('click', (event) => { stop(event); history = []; saveHistory(); renderHistory(shadow); });
    rail.addEventListener('mousedown', stop, true);
    close.addEventListener('mousedown', stop, true);
    clear.addEventListener('mousedown', stop, true);
    renderHistory(shadow);
    return shadow;
  }

  function renderHistory(ui) {
    const list = ui.querySelector('.history-list');
    const rail = ui.querySelector('.history-rail');
    const count = ui.querySelector('.rail-count');
    if (!list || !rail || !count) return;
    count.textContent = String(history.length);
    rail.classList.toggle('has-history', history.length > 0);
    if (!history.length) {
      list.innerHTML = `<div class="history-empty"><div class="history-empty-icon">${HISTORY_ICONS.Working}</div><strong>No activity yet</strong><span>Mac MCP actions on this tab will appear here as a clean visual timeline.</span></div>`;
      return;
    }
    list.innerHTML = '';
    history.slice().reverse().forEach((item) => {
      const row = document.createElement('div');
      row.className = 'history-item';
      const action = String(item.action || 'Working');
      const detailParts = [];
      const detail = safeDetail(item.detail);
      const target = safeTarget(item.target);
      if (detail) detailParts.push(detail);
      if (target && target !== 'Page') detailParts.push(target);
      else if (!detail && target) detailParts.push(target);
      row.innerHTML = `<div class="history-icon">${HISTORY_ICONS[action] || HISTORY_ICONS.Working}</div><div class="history-copy"><div class="history-action"></div><div class="history-detail"></div></div><div class="history-time"></div>`;
      row.querySelector('.history-action').textContent = historyLabel(action);
      row.querySelector('.history-detail').textContent = detailParts.join(' · ') || 'Page';
      row.querySelector('.history-time').textContent = formatClock(Number(item.ts || Date.now()));
      list.appendChild(row);
    });
  }

  function recordHistory(event, ui) {
    const action = HISTORY_LABELS[event.action] ? event.action : 'Working';
    const target = safeTarget(event.target_kind) || (['Inspecting', 'Finding', 'Reading', 'Scrolling'].includes(action) ? 'Page' : 'Control');
    const detail = safeDetail(event.detail);
    const now = Date.now();
    const next = { action, target, detail, ts: now };
    const previous = history[history.length - 1];
    if (previous && previous.action === next.action && previous.target === next.target && previous.detail === next.detail && now - Number(previous.ts || 0) < 700) {
      previous.ts = now;
    } else {
      history.push(next);
      if (history.length > MAX_HISTORY) history = history.slice(-MAX_HISTORY);
    }
    saveHistory();
    renderHistory(ui);
  }

  function decodeEvent() {
    const raw = document.documentElement && document.documentElement.getAttribute(EVENT_ATTR);
    if (!raw) return null;
    try { return JSON.parse(decodeURIComponent(escape(atob(raw)))); } catch (_) { return null; }
  }

  function render() {
    const event = decodeEvent();
    if (!event || event.claim !== true || event.seq === lastSeq) return;
    lastSeq = event.seq;
    const ui = ensureUI();
    const frame = ui.querySelector('.frame');
    const pill = ui.querySelector('.pill');
    const label = ui.querySelector('.label');
    const cursor = ui.querySelector('.cursor');
    const ripple = ui.querySelector('.ripple');
    const live = ui.querySelector('.history-live-status');
    const liveCopy = ui.querySelector('.history-live-copy');
    const phase = String(event.phase || 'working');
    const action = String(event.action || 'Working');

    recordHistory(event, ui);
    clearTimeout(hideTimer);
    frame.classList.toggle('attention', phase === 'attention');
    frame.classList.add('active');
    pill.classList.add('active');
    label.textContent = `Mac MCP · ${action}`;
    live.classList.add('active');
    liveCopy.textContent = historyLabel(action);

    const x = Number(event.x), y = Number(event.y);
    if (Number.isFinite(x) && Number.isFinite(y)) {
      cursor.classList.add('active');
      cursor.style.transform = `translate3d(${Math.round(x)}px,${Math.round(y)}px,0)`;
      clearTimeout(cursorTimer);
      cursorTimer = setTimeout(() => cursor.classList.remove('active'), 1100);
      if (event.effect === 'click') {
        ripple.classList.remove('go');
        ripple.style.left = `${Math.round(x)}px`;
        ripple.style.top = `${Math.round(y)}px`;
        void ripple.offsetWidth;
        ripple.classList.add('go');
      }
    }

    const ttl = Math.max(500, Math.min(Number(event.ttl_ms || 1600), 5000));
    const finish = () => {
      frame.classList.remove('active');
      pill.classList.remove('active');
      cursor.classList.remove('active');
      live.classList.remove('active');
      liveCopy.textContent = 'Ready';
    };
    hideTimer = setTimeout(finish, phase === 'done' ? 450 : ttl);
  }

  function install() {
    if (!document.documentElement) return requestAnimationFrame(install);
    window.addEventListener('mac-mcp-visual', render, true);
    new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: [EVENT_ATTR] });
    render();
  }
  install();
})();
