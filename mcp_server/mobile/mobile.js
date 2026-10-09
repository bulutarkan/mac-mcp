(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const LEGACY_STORAGE_KEY = "mac_mcp_mobile_session";
  // A non-secret marker so a later 401 can say access was lost instead of
  // looking like a first visit. The session itself stays in the HttpOnly cookie.
  const PAIRED_MARKER_KEY = "mac_mcp_mobile_paired";
  const state = {
    timer: null, agentCollapsed: null, activeAgents: null,
    lastSuccessAt: 0, online: null, unavailable: [], announced: "",
    loaded: { agents: false, activity: false, sessions: false },
    stopping: new Set(),
    armedStop: null,
    lastAgents: null,
    usageOpen: false, usageDays: 7, usageRequest: 0
  };

  function clearLegacySessionExposure() {
    try { localStorage.removeItem(LEGACY_STORAGE_KEY); } catch (_) {}
    try {
      const hash = location.hash.startsWith("#") ? location.hash.slice(1) : "";
      const params = new URLSearchParams(hash);
      if (params.has("session")) {
        params.delete("session");
        const suffix = params.toString();
        history.replaceState(null, "", location.pathname + location.search + (suffix ? "#" + suffix : ""));
      }
    } catch (_) {}
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
  function normalizeManualCode(value) {
    const raw = String(value || "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
    return raw.length > 4 ? raw.slice(0, 4) + "-" + raw.slice(4) : raw;
  }
  async function submitManualPairing(code) {
    const button = $("manualPairButton");
    const error = $("pairError");
    button.disabled = true;
    error.classList.add("hidden");
    try {
      const response = await fetch("/mobile/pair", {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          code: normalizeManualCode(code),
          device_name: deviceName()
        })
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status === 429) {
          error.textContent = "Too many attempts. Generate a new code on your Mac and try again shortly.";
        } else {
          error.textContent = "That pairing code is invalid, expired, or already used.";
        }
        error.classList.remove("hidden");
        return;
      }
      clearLegacySessionExposure();
      location.replace("/mobile");
    } catch (_) {
      error.textContent = "Couldn’t pair this device. Check the connection and try again.";
      error.classList.remove("hidden");
    } finally {
      button.disabled = false;
    }
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
  function svgIcon(name, cls="ui-icon") {
    const icons = {
      terminal: '<path d="M5 7l4 5-4 5M11 17h8"/>',
      browser: '<circle cx="12" cy="12" r="8.5"/><path d="m15.8 8.2-2.2 5.4-5.4 2.2 2.2-5.4 5.4-2.2Z"/><circle cx="12" cy="12" r="1.1" class="icon-fill"/>',
      file: '<path d="M7 3.5h6l4 4V20.5H7z"/><path d="M13 3.5v4h4M9.5 12h5M9.5 15h5"/>',
      search: '<circle cx="10.5" cy="10.5" r="5.5"/><path d="m14.6 14.6 4 4"/>',
      agent: '<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 2.5v2.5M15 2.5v2.5M9 19v2.5M15 19v2.5M2.5 9H5M2.5 15H5M19 9h2.5M19 15h2.5M9.5 10h5v4h-5z"/>',
      network: '<circle cx="12" cy="12" r="8.5"/><path d="M3.8 12h16.4M12 3.5c2.2 2.3 3.3 5.1 3.3 8.5S14.2 18.2 12 20.5M12 3.5C9.8 5.8 8.7 8.6 8.7 12s1.1 6.2 3.3 8.5"/>',
      code: '<path d="m9 7-5 5 5 5M15 7l5 5-5 5M13.5 5l-3 14"/>',
      observe: '<rect x="4" y="5" width="16" height="12" rx="2"/><path d="M9 20h6M12 17v3"/><circle cx="12" cy="11" r="2.2"/>',
      tool: '<path d="m14.2 6.2 3.6-2a4.4 4.4 0 0 1-5.5 5.5l-6.7 6.7a1.8 1.8 0 1 0 2.5 2.5l6.7-6.7a4.4 4.4 0 0 1 5.5-5.5l-2 3.6-4.1-4.1Z"/>',
      session: '<rect x="5" y="4" width="14" height="11" rx="2"/><path d="M8 18h8M8 20.5h8"/>'
    };
    return '<svg class="' + cls + '" viewBox="0 0 24 24" aria-hidden="true">' + (icons[name] || icons.tool) + '</svg>';
  }
  function toolIconName(value) {
    const v = String(value || "").toLowerCase();
    if (v.includes("browser") || v.includes("safari") || v.includes("chrome")) return "browser";
    if (v.includes("execute_js") || v.includes("javascript") || v.includes("script")) return "code";
    if (v.includes("command") || v.includes("terminal") || v === "bash" || v.includes("shell") || v.includes("applescript")) return "terminal";
    if (v.includes("read") || v.includes("write") || v.includes("edit") || v.includes("file")) return "file";
    if (v.includes("search") || v.includes("find")) return "search";
    if (v.includes("agent")) return "agent";
    if (v.includes("http") || v.includes("web") || v.includes("network")) return "network";
    if (v.includes("observe") || v.includes("screen") || v.includes("ui")) return "observe";
    return "tool";
  }
  function setAgentCollapsed(collapsed) {
    state.agentCollapsed = Boolean(collapsed);
    $("agentsCollapse").classList.toggle("collapsed", state.agentCollapsed);
    $("agentsToggle").setAttribute("aria-expanded", state.agentCollapsed ? "false" : "true");
  }
  function updateAgentDisclosure(activeCount) {
    const count = Math.max(0, Number(activeCount || 0));
    const idle = count === 0;
    const section = $("agentsSection");
    const toggle = $("agentsToggle");
    section.classList.toggle("collapsible", idle);
    toggle.disabled = !idle;
    if (!idle) {
      setAgentCollapsed(false);
    } else if (state.activeAgents !== 0 || state.agentCollapsed === null) {
      setAgentCollapsed(true);
    }
    state.activeAgents = count;
  }
  function agentRow(a) {
    const status = String(a.status || "");
    const running = status === "running" || status === "starting";
    const failed = status === "failed" || status === "error";
    const title = a.title || a.team_task_id || "Agent";
    const provider = [a.provider, a.model].filter(Boolean).join(" · ");
    const interactiveResource = Array.isArray(a.resource_activity)
      ? a.resource_activity.find((r) => ["browser_tab", "native_window", "native_app"].includes(String(r.kind || "")))
      : null;
    const detail = (interactiveResource && interactiveResource.label) || a.last_tool || a.phase || status || "Idle";
    const side = running ? duration(a.duration_ms) : (a.ended_at ? ago(a.ended_at) : duration(a.duration_ms));
    const detailIcon = toolIconName(a.last_tool || a.phase || status);
    return '<div class="agent">' +
      '<span class="row-icon agent-icon">' + svgIcon("agent") +
      '<span class="icon-state ' + (running ? "running" : (failed ? "failed" : "")) + '"></span></span>' +
      '<span class="sr-only">' + esc(running ? "Running" : (failed ? "Failed" : (status || "Idle"))) + '</span>' +
      '<div class="agent-main"><div class="agent-title"><strong>' + esc(title) + '</strong>' +
      (a.reasoning ? '<span class="badge">' + esc(a.reasoning) + '</span>' : '') +
      '</div><div class="agent-sub"><span>' + esc(provider) + '</span><span>·</span><span class="detail-with-icon">' +
      svgIcon(detailIcon, "mini-icon") + esc(detail) + '</span></div></div>' +
      '<div class="agent-side"><strong>' + esc(side) + '</strong>' +
      (a.tool_call_count ? '<span>' + esc(a.tool_call_count) + ' tools</span>' : '') + '</div>' +
      (running && a.agent_id ? stopButton(a) : '') + '</div>';
  }
  // Two taps: the first arms the button ("Stop?"), the second sends the stop.
  function stopButton(a) {
    const id = String(a.agent_id);
    const title = a.title || "agent";
    if (state.stopping.has(id)) {
      return '<span class="agent-stop busy" role="status">Stopping…</span>';
    }
    const armed = state.armedStop === id;
    return '<button type="button" class="agent-stop' + (armed ? ' armed' : '') + '" data-stop-agent="' + esc(id) +
      '" aria-label="' + esc(armed ? "Tap again to stop " + title : "Stop " + title) + '">' +
      (armed ? 'Stop?' : '<span class="stop-square" aria-hidden="true"></span>') + '</button>';
  }
  async function stopAgent(id) {
    state.stopping.add(id);
    state.armedStop = null;
    rerenderAgents();
    try {
      const r = await fetch("/mobile/api/agents/cancel", {
        method: "POST", cache: "no-store", credentials: "same-origin",
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: id })
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || ("HTTP " + r.status));
      announce(body.cancellation_state === "unconfirmed"
        ? "Stop requested; the agent has not exited yet."
        : "Agent stopped.");
    } catch (error) {
      announce("Could not stop the agent: " + error.message);
    } finally {
      state.stopping.delete(id);
      refresh();
    }
  }
  function rerenderAgents() {
    if (state.lastAgents) renderAgents(state.lastAgents);
  }
  function activityRow(e) {
    const t = e.timestamp || e.started_at || e.finished_at;
    const site = e.browser_context && e.browser_context.site ? e.browser_context.site : "";
    const sub = [e.source, site, ago(t)].filter(Boolean).join(" · ");
    const cls = e.status === "running" ? "running" : (e.status === "error" ? "error" : "");
    return '<div class="activity-row"><span class="row-icon activity-icon">' +
      svgIcon(toolIconName(e.tool)) + '</span><div class="activity-main"><strong>' + esc(e.tool || "Tool call") +
      '</strong><span>' + esc(sub) + '</span></div><div class="activity-side"><span>' +
      esc(duration(e.duration_ms)) + '</span><span class="result ' + cls + '" aria-hidden="true"></span>' +
      '<span class="sr-only">' + (cls === "running" ? "Running" : (cls === "error" ? "Failed" : "OK")) +
      '</span></div></div>';
  }
  function lifecycleLabel(value) {
    return ({
      ready:"Ready", queued:"Queued", delivered:"Delivered", acknowledged:"Acknowledged",
      failed:"Failed", disconnected:"Disconnected", expired:"Expired", unknown:"Needs attention"
    })[String(value || "").toLowerCase()] || "Ready";
  }
  function sessionSection(s) {
    const lifecycle = String(s.lifecycle_state || "").toLowerCase();
    if (s.needs_attention || ["failed","disconnected","expired","unknown"].includes(lifecycle)) return "attention";
    if (
      String(s.activity_state || s.state || "").toLowerCase() === "working" ||
      ["queued","delivered"].includes(lifecycle) ||
      Number(s.pending_instruction_count || s.queued || 0) > 0 ||
      Number(s.awaiting_acknowledgement_count || 0) > 0
    ) return "active";
    return "recent";
  }
  function sessionCard(s, section, terminal=false) {
    const lifecycle = lifecycleLabel(s.lifecycle_state);
    const title = terminal
      ? "Session ended"
      : (s.label || (s.flow_number ? "Session " + s.flow_number : "Agent session"));
    const detail = [s.detail || s.tool, lifecycle].filter(Boolean).join(" · ");
    const activity = terminal
      ? ago(s.transitioned_at || s.created_at)
      : (section === "active" && Number(s.activity_ms || 0) > 0
          ? duration(s.activity_ms)
          : ago(s.last_activity_at || s.last_transition_at || s.created_at));
    const pending = Number(s.pending_instruction_count || s.queued || 0);
    const awaiting = Number(s.awaiting_acknowledgement_count || 0);
    const queueText = pending ? pending + " queued" : (awaiting ? awaiting + " awaiting ack" : "");
    const iconName = terminal ? "session" : toolIconName([s.tool, s.detail, s.label].filter(Boolean).join(" "));
    return '<div class="session-card ' + section + '">' +
      '<span class="row-icon session-icon">' + svgIcon(iconName) + '<span class="session-status"></span></span>' +
      '<div class="session-main"><div class="session-title">' + esc(title) + '</div>' +
      '<div class="session-sub">' + esc(detail || "Session") + '</div></div>' +
      '<div class="session-side"><strong>' + esc(activity || lifecycle) + '</strong>' +
      '<span>' + esc(queueText || lifecycle) + '</span></div></div>';
  }
  function sessionGroup(key, title, subtitle, rows) {
    if (!rows.length) return "";
    return '<div class="session-group ' + key + '">' +
      '<div class="session-group-head"><div class="session-group-title"><strong>' + esc(title) +
      '</strong><span>' + esc(subtitle) + '</span></div><span class="session-group-count">' +
      rows.length + '</span></div><div class="session-list">' + rows.join("") + '</div></div>';
  }
  function renderSessions(data) {
    const sessions = Array.isArray(data.sessions) ? data.sessions : [];
    const ttlSeconds = Math.max(60, Number(data.session_ttl_minutes || 10) * 60);
    const cutoff = Date.now()/1000 - ttlSeconds;
    const groups = { attention: [], active: [], recent: [] };
    sessions.forEach(s => groups[sessionSection(s)].push(s));
    groups.attention.sort((a,b) => Number(b.last_transition_at || b.last_activity_at || 0) - Number(a.last_transition_at || a.last_activity_at || 0));
    groups.active.sort((a,b) => {
      const aw = String(a.activity_state || a.state || "") === "working";
      const bw = String(b.activity_state || b.state || "") === "working";
      return aw === bw
        ? Number(b.last_activity_at || 0) - Number(a.last_activity_at || 0)
        : (aw ? -1 : 1);
    });
    groups.recent = groups.recent
      .filter(s => Number(s.last_activity_at || s.created_at || 0) >= cutoff)
      .sort((a,b) => Number(b.last_activity_at || 0) - Number(a.last_activity_at || 0));

    const liveIDs = new Set(sessions.map(s => String(s.session_id || "")));
    const terminal = [];
    const seen = new Set();
    (Array.isArray(data.recent) ? data.recent : []).forEach(s => {
      const id = String(s.session_id || "");
      const transitioned = Number(s.transitioned_at || 0);
      if (!id || liveIDs.has(id) || seen.has(id) || !s.needs_attention || transitioned < cutoff) return;
      seen.add(id); terminal.push(s);
    });

    const attentionRows = groups.attention.map(s => sessionCard(s, "attention"))
      .concat(terminal.map(s => sessionCard(s, "attention", true)));
    const activeRows = groups.active.map(s => sessionCard(s, "active"));
    const recentRows = groups.recent.map(s => sessionCard(s, "recent"));
    const total = attentionRows.length + activeRows.length + recentRows.length;
    $("sessionMeta").textContent = activeRows.length ? activeRows.length + " active" : (total ? total + " visible" : "");
    $("sessions").innerHTML =
      sessionGroup("attention", "Needs Attention", "Review before continuing", attentionRows) +
      sessionGroup("active", "Active", "Working or awaiting delivery", activeRows) +
      sessionGroup("recent", "Recent", "Still within session retention", recentRows) ||
      '<div class="empty">No sessions yet.</div>';
  }
  function renderSessionError() {
    // Keep sessions already on screen and say they are out of date.
    if (state.loaded.sessions) {
      $("sessionMeta").textContent = "Unavailable · " + staleText();
      return;
    }
    $("sessionMeta").textContent = "Unavailable";
    $("sessions").innerHTML =
      '<div class="session-error">Sessions couldn’t be loaded.<br><button id="sessionRetry" type="button">Retry</button></div>';
    const retry = $("sessionRetry");
    if (retry) retry.addEventListener("click", () => refreshSessions());
  }
  async function refreshSessions() {
    try {
      const sessions = await api("/mobile/api/sessions");
      if (sessions.available === false) {
        renderSessionError();
        return false;
      }
      renderSessions(sessions);
      state.loaded.sessions = true;
      return true;
    } catch (e) {
      if (e.status === 401) throw e;
      renderSessionError();
      return false;
    }
  }
  function pairedBefore() {
    try { return localStorage.getItem(PAIRED_MARKER_KEY) === "1"; } catch (_) { return false; }
  }
  function rememberPaired(paired) {
    try {
      if (paired) localStorage.setItem(PAIRED_MARKER_KEY, "1");
      else localStorage.removeItem(PAIRED_MARKER_KEY);
    } catch (_) {}
  }
  function clearDashboard() {
    for (const id of ["agents", "activity", "sessions", "usageBody"]) $(id).innerHTML = "";
    for (const id of ["activeAgents", "calls1h", "successRate", "agentHint", "agentMeta", "sessionMeta", "activityMeta", "connection"]) $(id).textContent = "";
    state.lastSuccessAt = 0;
    state.loaded = { agents: false, activity: false, sessions: false };
  }
  function locked(reason="") {
    const accessLost = reason === "access_lost";
    clearDashboard();
    $("dashboard").classList.add("hidden");
    $("pairing").classList.remove("hidden");
    $("serverText").textContent = accessLost ? "Access expired or removed" : "Pairing required";
    $("version").textContent = "";
    $("connector").textContent = "Mobile";
    $("accessLost").classList.toggle("hidden", !accessLost);
    $("pairError").classList.toggle("hidden", reason !== "pair_error");
    $("pairRateLimited").classList.toggle("hidden", reason !== "rate_limited");
    if (accessLost) rememberPaired(false);
  }
  function unlocked() {
    rememberPaired(true);
    $("accessLost").classList.add("hidden");
    $("pairing").classList.add("hidden");
    $("dashboard").classList.remove("hidden");
  }
  function staleText() {
    return state.lastSuccessAt ? "showing data from " + ago(state.lastSuccessAt / 1000) : "no data yet";
  }
  function updatedText() {
    const age = ago(state.lastSuccessAt / 1000);
    return age === "now" ? "Updated just now" : "Updated " + age;
  }
  function announce(text) {
    // Announce changes of state only, not the ticking "Updated" time.
    if (text === state.announced) return;
    state.announced = text;
    $("announcer").textContent = text;
  }
  function renderConnection() {
    const row = $("connection");
    const parts = [];
    if (state.online) {
      parts.push("Mac reachable");
      state.unavailable.forEach(name => parts.push(name + " unavailable"));
      parts.push(updatedText());
    } else {
      parts.push("Can’t reach your Mac");
      parts.push(state.lastSuccessAt ? staleText() : "retrying");
    }
    row.textContent = parts.join(" · ");
    row.classList.toggle("degraded", !state.online || state.unavailable.length > 0);
    announce(state.online
      ? (state.unavailable.length ? "Mac reachable. " + state.unavailable.join(", ") + " unavailable." : "Mac reachable.")
      : "Can’t reach your Mac.");
  }
  function setOnline(online) {
    state.online = online;
    $("onlineDot").classList.toggle("online", online);
    $("serverText").textContent = online ? "Online" : "Connection unavailable";
  }
  function metric(value) {
    return value === null || value === undefined ? "—" : value;
  }
  function renderStatus(status) {
    $("version").textContent = status.version ? "v" + status.version : "";
    $("connector").textContent = status.connector ? connector(status.connector) : "—";
    $("activeAgents").textContent = metric(status.active_agents);
    $("calls1h").textContent = metric(status.calls_1h);
    $("successRate").textContent = status.success_rate === null || status.success_rate === undefined ? "—" : rate(status.success_rate);
    $("agentHint").textContent = status.active_agents === null || status.active_agents === undefined
      ? "Agent data unavailable"
      : (status.active_agents ? "Running on your Mac right now" : "Nothing running right now");
  }
  function renderAgents(agents) {
    state.lastAgents = agents;
    $("agentMeta").textContent = agents.active_count ? agents.active_count + " active" : ((agents.count || 0) ? "Recent" : "");
    $("agents").innerHTML = (agents.agents || []).map(agentRow).join("") || '<div class="empty">No delegated agents yet.</div>';
    updateAgentDisclosure(agents.active_count ?? 0);
    state.loaded.agents = true;
  }
  function renderActivity(activity) {
    const seen = new Set();
    const rows = [...(activity.active || []), ...(activity.events || [])].filter(e => {
      const key = e.event_id || ((e.tool || "") + ":" + (e.timestamp || e.started_at || ""));
      if (seen.has(key)) return false; seen.add(key); return true;
    }).slice(0, 8);
    $("activityMeta").textContent = "Last hour";
    $("activity").innerHTML = rows.map(activityRow).join("") || '<div class="empty">No tool activity in the last hour.</div>';
    state.loaded.activity = true;
  }
  function markUnavailable(section) {
    // A failed source keeps what was last shown instead of claiming "none".
    if (section === "agents") {
      $("agentMeta").textContent = "Unavailable";
      if (!state.loaded.agents) $("agents").innerHTML = '<div class="empty">Agent data is unavailable right now.</div>';
      $("agentsSection").classList.remove("collapsible");
      $("agentsToggle").disabled = true;
      setAgentCollapsed(false);
    } else {
      $("activityMeta").textContent = "Unavailable";
      if (!state.loaded.activity) $("activity").innerHTML = '<div class="empty">Activity is unavailable right now.</div>';
    }
  }
  async function settle(url) {
    try { return { ok: true, body: await api(url) }; }
    catch (error) { return { ok: false, error }; }
  }
  function lockOut() {
    clearLegacySessionExposure();
    locked(pairedBefore() ? "access_lost" : "");
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }
  async function refresh() {
    const [status, agents, activity] = await Promise.all([
      settle("/mobile/api/status"), settle("/mobile/api/agents"), settle("/mobile/api/activity")
    ]);
    if ([status, agents, activity].some(r => !r.ok && r.error.status === 401)) {
      lockOut();
      return;
    }
    if (!status.ok) {
      // The Mac itself is unreachable: keep the last values and say how old they are.
      setOnline(false);
      if (state.loaded.agents) $("agentMeta").textContent = "Stale";
      if (state.loaded.activity) $("activityMeta").textContent = "Stale";
      if (state.loaded.sessions) $("sessionMeta").textContent = "Stale";
      renderConnection();
      return;
    }
    unlocked();
    setOnline(true);
    state.lastSuccessAt = Date.now();
    renderStatus(status.body);
    const unavailable = [];
    const sources = status.body.sources || {};
    if (agents.ok && agents.body.available !== false) renderAgents(agents.body);
    else { markUnavailable("agents"); unavailable.push("Agent data"); }
    if (activity.ok && activity.body.available !== false) renderActivity(activity.body);
    else { markUnavailable("activity"); unavailable.push("Activity"); }
    if (sources.telemetry === "unavailable" && !unavailable.includes("Activity")) unavailable.push("Call metrics");
    try {
      if (!(await refreshSessions())) unavailable.push("Sessions");
    } catch (e) {
      if (e.status === 401) { lockOut(); return; }
    }
    state.unavailable = unavailable;
    renderConnection();
  }
  function formatCount(value) {
    const n = Number(value || 0);
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e4) return Math.round(n / 1e3) + "k";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }
  function renderUsage(data) {
    const body = $("usageBody");
    if (data.metering_enabled === false) {
      body.innerHTML = '<div class="empty">Usage metering is turned off on your Mac.</div>';
      return;
    }
    const totals = data.totals || {};
    if (!Number(totals.calls || 0)) {
      body.innerHTML = '<div class="empty">No tool calls in this period.</div>';
      return;
    }
    const tokens = Number(totals.input_tokens || 0) + Number(totals.output_tokens || 0);
    const days = (Array.isArray(data.daily) ? data.daily : []).slice().reverse().slice(0, 30);
    body.innerHTML =
      '<div class="usage-grid">' +
      '<div><span>Calls</span><strong>' + esc(formatCount(totals.calls)) + '</strong></div>' +
      '<div><span>Errors</span><strong>' + esc(formatCount(totals.error_count)) + '</strong></div>' +
      '<div><span>Payload tokens</span><strong>' + esc(formatCount(tokens)) + '</strong></div>' +
      '</div>' +
      (days.length ? '<details class="usage-daily"><summary>Daily breakdown</summary>' +
        days.map(d => '<div class="usage-day"><span>' + esc(d.date) + '</span><span>' +
          esc(formatCount(d.calls)) + ' calls · ' + esc(formatCount(d.error_count)) + ' errors · ' +
          esc(formatCount(Number(d.input_tokens || 0) + Number(d.output_tokens || 0))) + ' tokens</span></div>').join("") +
        '</details>' : '');
  }
  async function loadUsage() {
    const request = ++state.usageRequest;
    const body = $("usageBody");
    body.innerHTML = '<div class="empty">Loading usage…</div>';
    try {
      const data = await api("/mobile/api/usage?days=" + state.usageDays);
      if (request !== state.usageRequest) return;
      renderUsage(data);
    } catch (e) {
      if (request !== state.usageRequest) return;
      if (e.status === 401) { lockOut(); return; }
      body.innerHTML = '<div class="session-error">Usage couldn’t be loaded.<br><button id="usageRetry" type="button">Retry</button></div>';
      const retry = $("usageRetry");
      if (retry) retry.addEventListener("click", () => loadUsage());
    }
  }
  function setUsageOpen(open) {
    state.usageOpen = open;
    $("usagePanel").classList.toggle("hidden", !open);
    $("usageToggle").setAttribute("aria-expanded", open ? "true" : "false");
    $("usageToggle").textContent = open ? "Hide" : "Show";
    // Usage is fetched only when someone asks for it, never on the refresh timer.
    if (open) loadUsage();
  }
  async function boot() {
    clearLegacySessionExposure();
    const code = pairingCode();
    if (code) { submitPairing(code); return; }
    const u = new URL(location.href);
    const pairError = u.searchParams.get("pair_error");
    if (pairError) {
      history.replaceState(null, "", location.pathname);
      locked(pairError === "rate_limited" ? "rate_limited" : "pair_error");
      return;
    }
    await refresh();
    // Keep retrying while the Mac is unreachable; stop only when pairing is needed.
    if (!state.timer && $("pairing").classList.contains("hidden")) state.timer = setInterval(refresh, 4000);
  }
  const usageToggle = $("usageToggle");
  if (usageToggle) usageToggle.addEventListener("click", () => setUsageOpen(!state.usageOpen));
  // One delegated listener survives the list being re-rendered on every refresh.
  $("agents").addEventListener("click", (event) => {
    const button = event.target.closest("[data-stop-agent]");
    if (!button) return;
    const id = button.dataset.stopAgent;
    if (state.armedStop === id) {
      stopAgent(id);
      return;
    }
    state.armedStop = id;
    rerenderAgents();
    window.setTimeout(() => {
      if (state.armedStop === id) { state.armedStop = null; rerenderAgents(); }
    }, 6000);
  });
  document.querySelectorAll("[data-usage-days]").forEach(button => {
    button.addEventListener("click", () => {
      state.usageDays = Number(button.dataset.usageDays) || 7;
      document.querySelectorAll("[data-usage-days]").forEach(b =>
        b.setAttribute("aria-pressed", b === button ? "true" : "false"));
      loadUsage();
    });
  });
  const agentsToggle = $("agentsToggle");
  if (agentsToggle) {
    agentsToggle.addEventListener("click", () => {
      if (!$("agentsSection").classList.contains("collapsible")) return;
      setAgentCollapsed(!state.agentCollapsed);
    });
  }
  const manualPairInput = $("manualPairCode");
  const manualPairForm = $("manualPairForm");
  if (manualPairInput) {
    manualPairInput.addEventListener("input", () => {
      const formatted = normalizeManualCode(manualPairInput.value);
      if (manualPairInput.value !== formatted) manualPairInput.value = formatted;
    });
  }
  if (manualPairForm) {
    manualPairForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const code = normalizeManualCode(manualPairInput ? manualPairInput.value : "");
      if (code.replace("-", "").length !== 8) {
        $("pairError").textContent = "Enter the 8-character code shown in Mac MCP Settings.";
        $("pairError").classList.remove("hidden");
        return;
      }
      submitManualPairing(code);
    });
  }

  window.addEventListener("hashchange", () => {
    const code = pairingCode();
    if (code) submitPairing(code);
  });

  boot();
})();