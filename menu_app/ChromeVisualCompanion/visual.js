(() => {
  if (window.top !== window || window.__macMcpVisualCompanionLoaded) return;
  window.__macMcpVisualCompanionLoaded = true;

  const EVENT_ATTR = 'data-mac-mcp-visual-event';
  const HOST_ID = 'mac-mcp-visual-companion-root';
  const HISTORY_KEY = '__mac_mcp_visual_history_v1';
  const MAX_HISTORY = 30;
  const PANEL_ID = 'mac-mcp-activity-panel';
  // Screen readers hear session-level transitions only, never per-action telemetry.
  const ANNOUNCE_IDLE_MS = 8000;
  const ANNOUNCE_DELAY_MS = 150;
  const ANNOUNCEMENTS = {
    started: 'Mac MCP started working on this tab',
    attention: 'Mac MCP needs your attention on this tab',
    finished: 'Mac MCP finished working on this tab'
  };
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
  let announceTimer = null;
  let announceIdleTimer = null;
  let sessionAnnounced = false;
  let attentionAnnounced = false;
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
        .companion { --panel-w: min(328px, calc(100vw - 56px)); --surface: rgba(255,255,255,.95); --surface-2: rgba(244,245,248,.95); --surface-pill: rgba(255,255,255,.9); --fg: rgba(17,19,24,.94); --fg-2: rgba(17,19,24,.7); --fg-3: rgba(17,19,24,.6); --line: rgba(17,19,24,.12); --line-soft: rgba(17,19,24,.08); --fill: rgba(17,19,24,.045); --fill-hover: rgba(17,19,24,.08); --accent: rgb(0,113,206); --accent-fill: rgba(0,122,224,.1); --accent-line: rgba(0,122,224,.22); --badge-bg: rgb(0,113,206); --badge-fg: #fff; --focus: rgb(0,103,214); --shadow: rgba(15,20,30,.18); position: fixed; inset: 0; pointer-events: none; color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif; }
        @media (prefers-color-scheme: dark) { .companion { --surface: rgba(26,30,37,.95); --surface-2: rgba(15,18,23,.95); --surface-pill: rgba(17,20,25,.84); --fg: rgba(255,255,255,.96); --fg-2: rgba(255,255,255,.7); --fg-3: rgba(255,255,255,.6); --line: rgba(255,255,255,.14); --line-soft: rgba(255,255,255,.08); --fill: rgba(255,255,255,.055); --fill-hover: rgba(255,255,255,.1); --accent: rgb(116,211,255); --accent-fill: rgba(90,200,250,.12); --accent-line: rgba(90,200,250,.22); --badge-bg: rgba(90,200,250,.24); --badge-fg: rgb(196,235,255); --focus: rgba(116,211,255,.95); --shadow: rgba(0,0,0,.36); } }
        .frame { position: fixed; inset: 2px; border: 2px solid rgba(90, 200, 250, .78); border-radius: 10px; box-shadow: inset 0 0 0 1px rgba(255,255,255,.16), 0 0 18px rgba(90,200,250,.28); opacity: 0; transition: opacity .18s ease, border-color .18s ease, box-shadow .18s ease; animation: none; }
        .frame.active { opacity: 1; animation: mcpPulse 1.7s ease-in-out infinite; }
        .frame.attention { border-color: rgba(255, 184, 74, .9); box-shadow: 0 0 20px rgba(255,184,74,.28); }
        .pill { position: fixed; top: 14px; right: 14px; display: flex; align-items: center; gap: 8px; min-height: 28px; padding: 0 10px; border: 1px solid var(--line); border-radius: 999px; background: var(--surface-pill); color: var(--fg); box-shadow: 0 7px 24px var(--shadow); backdrop-filter: none; -webkit-backdrop-filter: none; font: 600 12px/1 -apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif; letter-spacing: -.01em; opacity: 0; transform: translateY(-5px) scale(.98); transition: opacity .16s ease, transform .16s ease, right .38s cubic-bezier(.22,.8,.2,1); }
        .pill.active { opacity: 1; transform: translateY(0) scale(1); backdrop-filter: blur(16px) saturate(140%); -webkit-backdrop-filter: blur(16px) saturate(140%); }
        .companion.sidebar-open .pill { right: calc(var(--panel-w) + 28px); }
        .dot { width: 7px; height: 7px; border-radius: 50%; background: rgb(90,200,250); box-shadow: 0 0 0 0 rgba(90,200,250,.45); animation: none; }
        .pill.active .dot { animation: mcpDot 1.35s ease-out infinite; }
        .label { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .cursor { position: fixed; left: 0; top: 0; width: 18px; height: 24px; opacity: 0; transform: translate3d(-40px,-40px,0); transition: transform .24s cubic-bezier(.22,.8,.2,1), opacity .12s ease; filter: drop-shadow(0 2px 4px rgba(0,0,0,.32)); }
        .cursor::before { content: ''; position: absolute; inset: 0; background: white; clip-path: polygon(0 0, 0 90%, 25% 69%, 40% 100%, 51% 94%, 36% 65%, 66% 64%); }
        .cursor::after { content: ''; position: absolute; inset: 1px; background: #111; clip-path: polygon(0 0, 0 90%, 25% 69%, 40% 100%, 51% 94%, 36% 65%, 66% 64%); z-index: -1; transform: scale(1.08); transform-origin: top left; }
        .cursor.active { opacity: .96; }
        .ripple { position: fixed; width: 12px; height: 12px; margin: -6px 0 0 -6px; border-radius: 50%; border: 2px solid rgba(90,200,250,.9); opacity: 0; transform: scale(.35); }
        .ripple.go { animation: mcpRipple .52s ease-out forwards; }

        .history-rail { position: fixed; right: 0; top: 50%; width: 30px; min-height: 78px; padding: 10px 0 8px; transform: translateY(-50%); pointer-events: auto; border: 1px solid var(--line); border-right: 0; border-radius: 14px 0 0 14px; background: var(--surface); color: var(--fg-2); box-shadow: -4px 6px 18px var(--shadow); backdrop-filter: blur(14px) saturate(135%); -webkit-backdrop-filter: blur(14px) saturate(135%); opacity: .9; cursor: pointer; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; font: inherit; transition: opacity .18s ease, width .18s ease, right .38s cubic-bezier(.22,.8,.2,1), background .18s ease; }
        .history-rail::after { content: ''; position: absolute; inset: -10px 0 -10px -12px; }
        .history-rail:hover { opacity: 1; width: 34px; }
        .history-rail .rail-icon { display: flex; }
        .history-rail .rail-icon svg { width: 16px; height: 16px; fill: none; stroke: var(--accent); stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
        .history-rail .rail-chevron { width: 6px; height: 6px; margin-top: 1px; border-left: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; transform: translateX(1px) rotate(45deg); opacity: .75; transition: transform .18s ease; }
        .history-rail .rail-count { display: none; min-width: 20px; height: 18px; padding: 0 5px; border-radius: 999px; align-items: center; justify-content: center; background: var(--badge-bg); color: var(--badge-fg); font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1; }
        .history-rail.has-history .rail-count { display: flex; }
        .companion.sidebar-open .history-rail { right: calc(var(--panel-w) + 12px); opacity: 1; }
        .companion.sidebar-open .history-rail .rail-chevron { transform: translateX(-1px) rotate(225deg); }

        .history-panel { position: fixed; top: 12px; right: 12px; bottom: 12px; width: var(--panel-w); pointer-events: none; overflow: hidden; display: flex; flex-direction: column; border-radius: 22px; border: 1px solid var(--line); background: linear-gradient(180deg, var(--surface), var(--surface-2)); color: var(--fg); box-shadow: 0 24px 68px var(--shadow), inset 0 1px 0 rgba(255,255,255,.07); backdrop-filter: none; -webkit-backdrop-filter: none; visibility: hidden; opacity: 0; transform: translate3d(34px,0,0) scale(.985); transform-origin: right center; transition: opacity .23s ease, transform .38s cubic-bezier(.22,.8,.2,1); }
        .companion.sidebar-open .history-panel { pointer-events: auto; visibility: visible; opacity: 1; transform: translate3d(0,0,0) scale(1); backdrop-filter: blur(28px) saturate(135%); -webkit-backdrop-filter: blur(28px) saturate(135%); }
        .history-header { flex: 0 0 auto; padding: 18px 18px 14px; border-bottom: 1px solid var(--line-soft); }
        .history-header-row { display: flex; align-items: center; gap: 10px; }
        .history-brand { width: 32px; height: 32px; border-radius: 10px; display: flex; align-items: center; justify-content: center; background: var(--accent-fill); border: 1px solid var(--accent-line); }
        .history-brand svg { width: 18px; height: 18px; fill: none; stroke: var(--accent); stroke-width: 1.65; stroke-linecap: round; stroke-linejoin: round; }
        .history-titles { min-width: 0; flex: 1; }
        .history-title { color: var(--fg); font-size: 14px; font-weight: 700; letter-spacing: -.015em; line-height: 1.1; }
        .history-subtitle { margin-top: 4px; color: var(--fg-2); font-size: 12px; font-weight: 500; }
        .history-close { width: 28px; height: 28px; padding: 0; border: 0; border-radius: 9px; color: var(--fg-2); background: var(--fill); cursor: pointer; font-size: 18px; line-height: 28px; transition: color .16s ease, background .16s ease; }
        .history-close:hover { color: var(--fg); background: var(--fill-hover); }
        .history-live { margin-top: 13px; display: flex; align-items: center; justify-content: space-between; gap: 10px; }
        .history-live-status { display: flex; align-items: center; gap: 7px; color: var(--fg-2); font-size: 12px; font-weight: 600; }
        .history-live-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--fg-3); transition: background .18s ease, box-shadow .18s ease; }
        .history-live-status.active .history-live-dot { background: rgb(90,200,250); box-shadow: 0 0 0 4px rgba(90,200,250,.16); }
        .history-clear { padding: 5px 8px; border: 0; border-radius: 8px; background: transparent; color: var(--fg-2); font-size: 12px; font-weight: 600; cursor: pointer; transition: color .16s ease, background .16s ease; }
        .history-clear:hover { color: var(--fg); background: var(--fill); }
        .history-list { flex: 1 1 auto; overflow-y: auto; padding: 10px 12px 14px; scrollbar-width: thin; scrollbar-color: var(--line) transparent; }
        .history-empty { height: 100%; min-height: 180px; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 24px; color: var(--fg-2); }
        .history-empty-icon { width: 40px; height: 40px; border-radius: 14px; margin-bottom: 12px; display: flex; align-items: center; justify-content: center; border: 1px solid var(--line-soft); background: var(--fill); }
        .history-empty-icon svg { width: 20px; height: 20px; fill: none; stroke: var(--fg-3); stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
        .history-empty strong { color: var(--fg); font-size: 13px; margin-bottom: 5px; }
        .history-empty span { max-width: 220px; font-size: 12px; line-height: 1.45; }
        .history-item { position: relative; display: grid; grid-template-columns: 34px minmax(0,1fr) auto; gap: 10px; align-items: start; padding: 9px 8px; border-radius: 13px; transition: background .14s ease; }
        .history-item:hover { background: var(--fill); }
        .history-item + .history-item::before { content: ''; position: absolute; left: 24.5px; top: -8px; width: 1px; height: 10px; background: var(--line); }
        .history-icon { width: 32px; height: 32px; border-radius: 10px; display: flex; align-items: center; justify-content: center; background: var(--fill); border: 1px solid var(--line-soft); }
        .history-icon svg { width: 16px; height: 16px; fill: none; stroke: var(--accent); stroke-width: 1.55; stroke-linecap: round; stroke-linejoin: round; }
        .history-copy { min-width: 0; padding-top: 1px; }
        .history-action { color: var(--fg); font-size: 13px; font-weight: 650; letter-spacing: -.005em; line-height: 1.2; }
        .history-detail { margin-top: 3px; color: var(--fg-2); font-size: 12px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .history-time { padding-top: 2px; color: var(--fg-3); font-size: 11px; font-variant-numeric: tabular-nums; }
        .history-footer { flex: 0 0 auto; padding: 11px 16px 13px; border-top: 1px solid var(--line-soft); display: flex; align-items: center; gap: 7px; color: var(--fg-3); font-size: 11px; }
        .history-footer-dot { width: 4px; height: 4px; border-radius: 50%; background: rgba(90,200,250,.7); }
        .history-rail:focus-visible, .history-close:focus-visible, .history-clear:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
        .history-rail:focus-visible { opacity: 1; outline-offset: -2px; }
        .sr-status { position: fixed; width: 1px; height: 1px; margin: -1px; padding: 0; border: 0; overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; }

        @keyframes mcpPulse { 0%,100% { box-shadow: 0 0 12px rgba(90,200,250,.16); } 50% { box-shadow: 0 0 24px rgba(90,200,250,.38); } }
        @keyframes mcpDot { 0% { box-shadow: 0 0 0 0 rgba(90,200,250,.48); } 65%,100% { box-shadow: 0 0 0 6px rgba(90,200,250,0); } }
        @keyframes mcpRipple { 0% { opacity: .92; transform: scale(.35); } 100% { opacity: 0; transform: scale(2.8); } }
        @media (max-width: 520px) { .companion.sidebar-open .pill { opacity: 0; } }
        @media (prefers-reduced-motion: reduce) { .frame,.dot { animation: none; } .cursor,.pill,.history-panel,.history-rail { transition-duration: .01ms; } }
      </style>
      <div class="companion">
        <div class="frame" aria-hidden="true"></div>
        <div class="pill" aria-hidden="true"><span class="dot"></span><span class="label">Mac MCP · Working</span></div>
        <div class="cursor" aria-hidden="true"></div>
        <div class="ripple" aria-hidden="true"></div>
        <div class="sr-status" role="status" aria-live="polite" aria-atomic="true"></div>
        <button class="history-rail" type="button" aria-label="Mac MCP tab activity" aria-expanded="false" aria-controls="${PANEL_ID}">
          <span class="rail-icon" aria-hidden="true"><svg viewBox="0 0 20 20"><path d="M2.5 10.5h3.2l2.1-5 4.2 9.5 2.2-4.5h3.3"/></svg></span><span class="rail-count" aria-hidden="true">0</span><span class="rail-chevron" aria-hidden="true"></span>
        </button>
        <aside class="history-panel" id="${PANEL_ID}" aria-label="Mac MCP tab activity">
          <div class="history-header">
            <div class="history-header-row">
              <div class="history-brand"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5.1 4.8h9.8v10.4H5.1z"/><path d="M7.7 8h4.6M7.7 11h4.6"/></svg></div>
              <div class="history-titles"><div class="history-title">Mac MCP</div><div class="history-subtitle">Tab activity</div></div>
              <button class="history-close" type="button" aria-label="Close activity panel">×</button>
            </div>
            <div class="history-live">
              <div class="history-live-status"><span class="history-live-dot"></span><span class="history-live-copy">Ready</span></div>
              <button class="history-clear" type="button" aria-label="Clear tab activity">Clear</button>
            </div>
          </div>
          <div class="history-list"></div>
          <div class="history-footer"><span class="history-footer-dot"></span><span>This tab only · no page content stored</span></div>
        </aside>
      </div>`;
    (document.documentElement || document).appendChild(host);

    const companion = shadow.querySelector('.companion');
    const rail = shadow.querySelector('.history-rail');
    const panel = shadow.querySelector('.history-panel');
    const close = shadow.querySelector('.history-close');
    const clear = shadow.querySelector('.history-clear');
    const stop = (event) => { event.preventDefault(); event.stopPropagation(); };
    const setSidebarOpen = (open) => {
      companion.classList.toggle('sidebar-open', open);
      rail.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    // Only return focus to the rail when keyboard focus was inside the panel; mouse use never moves page focus.
    const closeSidebar = () => {
      const focusWasInPanel = panel.contains(shadow.activeElement);
      setSidebarOpen(false);
      if (focusWasInPanel) rail.focus({ preventScroll: true });
    };
    rail.addEventListener('click', (event) => { stop(event); setSidebarOpen(!companion.classList.contains('sidebar-open')); });
    close.addEventListener('click', (event) => { stop(event); closeSidebar(); });
    clear.addEventListener('click', (event) => { stop(event); history = []; saveHistory(); renderHistory(shadow); });
    panel.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !companion.classList.contains('sidebar-open')) return;
      stop(event);
      closeSidebar();
    });
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
    rail.setAttribute('aria-label', history.length ? `Mac MCP tab activity, ${history.length} ${history.length === 1 ? 'entry' : 'entries'}` : 'Mac MCP tab activity');
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

  function announce(ui, message) {
    const region = ui.querySelector('.sr-status');
    if (!region) return;
    clearTimeout(announceTimer);
    // Delay so a freshly mounted live region is registered before its text changes.
    announceTimer = setTimeout(() => { region.textContent = message; }, ANNOUNCE_DELAY_MS);
  }

  function updateAnnouncements(ui, phase, ttl) {
    clearTimeout(announceIdleTimer);
    if (phase === 'attention') {
      if (!attentionAnnounced) announce(ui, ANNOUNCEMENTS.attention);
      attentionAnnounced = true;
    } else {
      attentionAnnounced = false;
      if (!sessionAnnounced) announce(ui, ANNOUNCEMENTS.started);
    }
    sessionAnnounced = true;
    announceIdleTimer = setTimeout(() => {
      sessionAnnounced = false;
      attentionAnnounced = false;
      announce(ui, ANNOUNCEMENTS.finished);
    }, phase === 'done' ? 450 : Math.max(ttl, ANNOUNCE_IDLE_MS));
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
    updateAnnouncements(ui, phase, ttl);
  }

  function install() {
    if (!document.documentElement) return requestAnimationFrame(install);
    window.addEventListener('mac-mcp-visual', render, true);
    new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: [EVENT_ATTR] });
    render();
  }
  install();
})();
