(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const LEGACY_STORAGE_KEY = "mac_mcp_mobile_session";
  const state = { timer: null };

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
    return '<div class="session-card ' + section + '">' +
      '<span class="session-status"></span>' +
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
    $("sessionMeta").textContent = "Unavailable";
    $("sessions").innerHTML =
      '<div class="session-error">Sessions couldn’t be loaded.<br><button id="sessionRetry" type="button">Retry</button></div>';
    const retry = $("sessionRetry");
    if (retry) retry.addEventListener("click", () => refreshSessions());
  }
  async function refreshSessions() {
    try {
      const sessions = await api("/mobile/api/sessions");
      renderSessions(sessions);
    } catch (e) {
      if (e.status === 401) throw e;
      renderSessionError();
    }
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
      await refreshSessions();
    } catch (e) {
      if (e.status === 401) {
        clearLegacySessionExposure();
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
    clearLegacySessionExposure();
    const code = pairingCode();
    if (code) { submitPairing(code); return; }
    const u = new URL(location.href);
    if (u.searchParams.get("pair_error")) {
      history.replaceState(null, "", location.pathname); locked(true); return;
    }
    await refresh();
    if (!state.timer && !$("dashboard").classList.contains("hidden")) state.timer = setInterval(refresh, 4000);
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