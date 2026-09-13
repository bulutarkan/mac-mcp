(() => {
  if (window.top !== window || window.__macMcpVisualCompanionLoaded) return;
  window.__macMcpVisualCompanionLoaded = true;

  const EVENT_ATTR = 'data-mac-mcp-visual-event';
  const HOST_ID = 'mac-mcp-visual-companion-root';
  let hideTimer = null;
  let cursorTimer = null;
  let lastSeq = null;

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
        .frame { position: fixed; inset: 2px; border: 2px solid rgba(90, 200, 250, .78); border-radius: 10px; box-shadow: inset 0 0 0 1px rgba(255,255,255,.16), 0 0 18px rgba(90,200,250,.28); opacity: 0; transition: opacity .18s ease, border-color .18s ease, box-shadow .18s ease; animation: mcpPulse 1.7s ease-in-out infinite; }
        .frame.active { opacity: 1; }
        .frame.attention { border-color: rgba(255, 184, 74, .9); box-shadow: 0 0 20px rgba(255,184,74,.28); }
        .pill { position: fixed; top: 14px; right: 14px; display: flex; align-items: center; gap: 8px; min-height: 28px; padding: 0 10px; border: 1px solid rgba(255,255,255,.18); border-radius: 999px; background: rgba(17,20,25,.78); color: rgba(255,255,255,.94); box-shadow: 0 7px 24px rgba(0,0,0,.22); backdrop-filter: blur(16px) saturate(140%); -webkit-backdrop-filter: blur(16px) saturate(140%); font: 600 12px/1 -apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif; letter-spacing: -.01em; opacity: 0; transform: translateY(-5px) scale(.98); transition: opacity .16s ease, transform .16s ease; }
        .pill.active { opacity: 1; transform: translateY(0) scale(1); }
        .dot { width: 7px; height: 7px; border-radius: 50%; background: rgb(90,200,250); box-shadow: 0 0 0 0 rgba(90,200,250,.45); animation: mcpDot 1.35s ease-out infinite; }
        .label { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .cursor { position: fixed; left: 0; top: 0; width: 18px; height: 24px; opacity: 0; transform: translate3d(-40px,-40px,0); transition: transform .24s cubic-bezier(.22,.8,.2,1), opacity .12s ease; filter: drop-shadow(0 2px 4px rgba(0,0,0,.32)); }
        .cursor::before { content: ''; position: absolute; inset: 0; background: white; clip-path: polygon(0 0, 0 90%, 25% 69%, 40% 100%, 51% 94%, 36% 65%, 66% 64%); }
        .cursor::after { content: ''; position: absolute; inset: 1px; background: #111; clip-path: polygon(0 0, 0 90%, 25% 69%, 40% 100%, 51% 94%, 36% 65%, 66% 64%); z-index: -1; transform: scale(1.08); transform-origin: top left; }
        .cursor.active { opacity: .96; }
        .ripple { position: fixed; width: 12px; height: 12px; margin: -6px 0 0 -6px; border-radius: 50%; border: 2px solid rgba(90,200,250,.9); opacity: 0; transform: scale(.35); }
        .ripple.go { animation: mcpRipple .52s ease-out forwards; }
        @keyframes mcpPulse { 0%,100% { box-shadow: 0 0 12px rgba(90,200,250,.16); } 50% { box-shadow: 0 0 24px rgba(90,200,250,.38); } }
        @keyframes mcpDot { 0% { box-shadow: 0 0 0 0 rgba(90,200,250,.48); } 65%,100% { box-shadow: 0 0 0 6px rgba(90,200,250,0); } }
        @keyframes mcpRipple { 0% { opacity: .92; transform: scale(.35); } 100% { opacity: 0; transform: scale(2.8); } }
        @media (prefers-reduced-motion: reduce) { .frame,.dot { animation: none; } .cursor { transition: none; } }
      </style>
      <div class="frame"></div>
      <div class="pill"><span class="dot"></span><span class="label">Mac MCP · Working</span></div>
      <div class="cursor"></div>
      <div class="ripple"></div>`;
    (document.documentElement || document).appendChild(host);
    return shadow;
  }

  function decodeEvent() {
    const raw = document.documentElement && document.documentElement.getAttribute(EVENT_ATTR);
    if (!raw) return null;
    try { return JSON.parse(decodeURIComponent(escape(atob(raw)))); } catch (_) { return null; }
  }

  function render() {
    const event = decodeEvent();
    if (!event || event.seq === lastSeq) return;
    lastSeq = event.seq;
    const ui = ensureUI();
    const frame = ui.querySelector('.frame');
    const pill = ui.querySelector('.pill');
    const label = ui.querySelector('.label');
    const cursor = ui.querySelector('.cursor');
    const ripple = ui.querySelector('.ripple');
    const phase = String(event.phase || 'working');
    const action = String(event.action || 'Working');

    clearTimeout(hideTimer);
    frame.classList.toggle('attention', phase === 'attention');
    frame.classList.add('active');
    pill.classList.add('active');
    label.textContent = `Mac MCP · ${action}`;

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
    if (phase === 'done') {
      hideTimer = setTimeout(() => { frame.classList.remove('active'); pill.classList.remove('active'); cursor.classList.remove('active'); }, 450);
    } else {
      hideTimer = setTimeout(() => { frame.classList.remove('active'); pill.classList.remove('active'); cursor.classList.remove('active'); }, ttl);
    }
  }

  function install() {
    if (!document.documentElement) return requestAnimationFrame(install);
    ensureUI();
    window.addEventListener('mac-mcp-visual', render, true);
    new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: [EVENT_ATTR] });
    render();
  }
  install();
})();
