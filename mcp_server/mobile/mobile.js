(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "mac_mcp_mobile_session";
  const state = { timer: null };

  function token() {
    try { return localStorage.getItem(STORAGE_KEY) || ""; } catch (_) { return ""; }
  }
  function clearToken() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
  }
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, c => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[c]));
  }
  function deviceName() {
    const ua = navigator.userAgent || "";
    if (/iPad/i.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "iPad";
    if (/iPhone/i.test(ua)) return "iPhone";
    return "Mobile device";
  }
  async function api(url) {
    const headers = { "Accept": "application/json" };
    const t = token();
    if (t) headers.Authorization = "Bearer " + t;
    const r = await fetch(url, { cache: "no-store", credentials: "same-origin", headers });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(body.error || ("HTTP " + r.status));
      e.status = r.status;
      throw e;
    }
    return body;
  }
  function pairingCode() {
    const hash = location.hash.startsWith("#") ? location.hash.slice(1) : "";
    const code = new URLSearchParams(hash).get("pair");
    if (code) history.replaceState(null, "", location.pathname);
    return code;
  }
  function submitPairing(code) {
    const form = document.createElement("form");
    form.method = "POST"; form.action = "/mobile/pair"; form.style.display = "none";
    for (const [name, value] of [["code", code], ["device_name", deviceName()]]) {
      const input = document.createElement("input");
      input.type = "hidden"; input.name = name; input.value = value; form.appendChild(input);
    }
    document.body.appendChild(form); form.submit();
  }
  function duration(ms) {
    const n = Number(ms || 0);
    if (!n) return "";
    if (n < 1000) return Math.round(n) + "ms";
    const s = Math.round(n / 1000);
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    return m < 60 ? m + "m" : Math.floor(m / 60) + "h";
  }
  function ago(epoch) {
    if (!epoch) return "";
    const d = Math.max(0, Math.floor(Date.now()/1000 - Number(epoch)));
    if (d < 10) return "now";
    if (d < 60) return d + "s ago";
    if (d < 3600) return Math.floor(d/60) + "m ago";
    if (d < 86400) return Math.floor(d/3600) + "h ago";
    return Math.floor(d/86400) + "d ago";
  }
  function connector(v) {
    return ({cloudflare:"Cloudflare",ngrok:"ngrok",custom:"HTTPS",none:"Local"})[String(v||"").toLowerCase()] || "Remote";
  }
  function rate(v) {
    const n = Number(v); if (!Number.isFinite(n)) return "—";
    return (Math.round(n*10)%10===0 ? Math.round(n) : n.toFixed(1)) + "%";
  }
  function agentRow(a) {
    const status = String(a.status || "");
    const running = status === "running" || status === "starting";
    const failed = status === "failed" || status === "error";
    const title = a.title || a.team_task_id || "Agent";
    const provider = [a.provider, a.model].filter(Boolean).join(" · ");
    const detail = a.last_tool || a.phase || status || "Idle";
    const side = running ? duration(a.duration_ms) : (a.ended_at ? ago(a.ended_at) : duration(a.duration_ms));
    return '<div class="agent">' +
      '<span class="state ' + (running ? "running" : (failed ? "failed" : "")) + '"></span>' +
      '<div class="agent-main"><div class="agent-title"><strong>' + esc(title) + '</strong>' +
      (a.reasoning ? '<span class="badge">' + esc(a.reasoning) + '</span>' : '') +
      '</div><div class="agent-sub"><span>' + esc(provider) + '</span><span>·</span><span>' + esc(detail) + '</span></div></div>' +
      '<div class="agent-side"><strong>' + esc(side) + '</strong>' +
      (a.tool_call_count ? '<span>' + esc(a.tool_call_count) + ' tools</span>' : '') + '</div></div>';
  }
  function activityRow(e) {
    const t = e.timestamp || e.started_at || e.finished_at;
    const site = e.browser_context && e.browser_context.site ? e.browser_context.site : "";
    const sub = [e.source, site, ago(t)].filter(Boolean).join(" · ");
    const cls = e.status === "running" ? "running" : (e.status === "error" ? "error" : "");
    return '<div class="activity-row"><div class="activity-main"><strong>' + esc(e.tool || "Tool call") +
      '</strong><span>' + esc(sub) + '</span></div><div class="activity-side"><span>' +
      esc(duration(e.duration_ms)) + '</span><span class="result ' + cls + '"></span></div></div>';
  }
  function locked(showError=false) {
    $("dashboard").classList.add("hidden");
    $("pairing").classList.remove("hidden");
    $("serverText").textContent = "Pairing required";
    $("version").textContent = "";
    $("connector").textContent = "Mobile";
    $("pairError").classList.toggle("hidden", !showError);
  }
  function unlocked() {
    $("pairing").classList.add("hidden");
    $("dashboard").classList.remove("hidden");
  }
  async function refresh() {
    try {
      const [status, agents, activity] = await Promise.all([
        api("/mobile/api/status"), api("/mobile/api/agents"), api("/mobile/api/activity")
      ]);
      unlocked();
      $("serverText").textContent = "Online";
      $("version").textContent = status.version ? "v" + status.version : "";
      $("connector").textContent = connector(status.connector);
      $("activeAgents").textContent = status.active_agents ?? 0;
      $("calls1h").textContent = status.calls_1h ?? 0;
      $("successRate").textContent = rate(status.success_rate);
      $("agentHint").textContent = status.active_agents ? "Running on your Mac right now" : "Nothing running right now";
      $("agentMeta").textContent = agents.active_count ? agents.active_count + " active" : ((agents.count || 0) ? "Recent" : "");
      $("agents").innerHTML = (agents.agents || []).map(agentRow).join("") || '<div class="empty">No delegated agents yet.</div>';
      const seen = new Set();
      const rows = [...(activity.active || []), ...(activity.events || [])].filter(e => {
        const key = e.event_id || ((e.tool || "") + ":" + (e.timestamp || e.started_at || ""));
        if (seen.has(key)) return false; seen.add(key); return true;
      }).slice(0, 8);
      $("activity").innerHTML = rows.map(activityRow).join("") || '<div class="empty">No tool activity in the last hour.</div>';
    } catch (e) {
      if (e.status === 401) {
        clearToken();
        locked(false);
        if (state.timer) clearInterval(state.timer);
        state.timer = null;
      } else {
        $("serverText").textContent = "Connection unavailable";
        $("onlineDot").classList.remove("online");
      }
    }
  }
  async function boot() {
    const code = pairingCode();
    if (code) { submitPairing(code); return; }
    const u = new URL(location.href);
    if (u.searchParams.get("pair_error")) {
      history.replaceState(null, "", location.pathname); locked(true); return;
    }
    await refresh();
    if (!state.timer && !$("dashboard").classList.contains("hidden")) state.timer = setInterval(refresh, 4000);
  }
  window.addEventListener("hashchange", () => {
    const code = pairingCode();
    if (code) submitPairing(code);
  });

  boot();
})();