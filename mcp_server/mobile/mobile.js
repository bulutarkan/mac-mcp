(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const state = { timer: null };

  function escapeHTML(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
  }

  function deviceName() {
    const ua = navigator.userAgent || "";
    if (/iPad/i.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "iPad";
    if (/iPhone/i.test(ua)) return "iPhone";
    return "Mobile device";
  }

  async function json(url) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "application/json" },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.error || ("HTTP " + response.status));
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function pairingCode() {
    const hash = location.hash.startsWith("#") ? location.hash.slice(1) : "";
    const code = new URLSearchParams(hash).get("pair");
    if (code) {
      history.replaceState(null, "", location.pathname);
    }
    return code;
  }

  function submitPairing(code) {
    const form = document.createElement("form");
    form.method = "POST";
    form.action = "/mobile/pair";
    form.style.display = "none";

    const codeInput = document.createElement("input");
    codeInput.type = "hidden";
    codeInput.name = "code";
    codeInput.value = code;

    const deviceInput = document.createElement("input");
    deviceInput.type = "hidden";
    deviceInput.name = "device_name";
    deviceInput.value = deviceName();

    form.append(codeInput, deviceInput);
    document.body.appendChild(form);

    // A top-level navigation makes WebKit commit the persistent HttpOnly cookie
    // before the redirected /mobile page loads. This is more reliable than
    // setting the cookie through an in-page fetch after scanning a Camera QR.
    form.submit();
  }

  function formatDuration(ms) {
    if (ms == null) return "";
    const n = Math.max(0, Number(ms) || 0);
    if (n < 1000) return Math.round(n) + "ms";
    const seconds = Math.round(n / 1000);
    if (seconds < 60) return seconds + "s";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + "m";
    return Math.floor(minutes / 60) + "h";
  }

  function timeAgo(epochSeconds) {
    if (epochSeconds == null) return "";
    const delta = Math.max(0, Math.floor(Date.now() / 1000 - Number(epochSeconds)));
    if (delta < 10) return "now";
    if (delta < 60) return delta + "s ago";
    if (delta < 3600) return Math.floor(delta / 60) + "m ago";
    if (delta < 86400) return Math.floor(delta / 3600) + "h ago";
    return Math.floor(delta / 86400) + "d ago";
  }

  function connectorName(value) {
    switch (String(value || "").toLowerCase()) {
      case "cloudflare": return "Cloudflare";
      case "ngrok": return "ngrok";
      case "custom": return "HTTPS";
      default: return "Local";
    }
  }

  function formatRate(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return (Math.round(n * 10) % 10 === 0 ? Math.round(n) : n.toFixed(1)) + "%";
  }

  function agentStatusClass(status) {
    if (status === "starting" || status === "running") return "running";
    if (status === "failed" || status === "error") return "failed";
    return "";
  }

  function agentStatusGlyph(status) {
    if (status === "starting" || status === "running") return "";
    if (status === "failed" || status === "error") return "!";
    return "✓";
  }

  function agentRow(agent) {
    const status = String(agent.status || "");
    const active = status === "starting" || status === "running";
    const title = agent.title || agent.team_task_id || "Agent";
    const provider = agent.provider || "Agent";
    const model = agent.model || "";
    const reasoning = agent.reasoning ? String(agent.reasoning) : "";
    const detail = agent.last_tool || agent.phase || status || "Idle";
    const sideTime = active
      ? formatDuration(agent.duration_ms)
      : (agent.ended_at ? timeAgo(agent.ended_at) : formatDuration(agent.duration_ms));
    const calls = Number(agent.tool_call_count || 0);

    return '<div class="agent-row">' +
      '<span class="status-orb ' + agentStatusClass(status) + '">' + escapeHTML(agentStatusGlyph(status)) + '</span>' +
      '<div class="agent-main">' +
        '<div class="agent-titleline">' +
          '<span class="agent-name">' + escapeHTML(title) + '</span>' +
          '<span class="agent-provider">' + escapeHTML(provider + (model ? " · " + model : "")) + '</span>' +
          (reasoning ? '<span class="badge">' + escapeHTML(reasoning) + '</span>' : '') +
        '</div>' +
        '<div class="agent-subline"><span class="tool-mini">▣</span><span class="ellipsis">' + escapeHTML(detail) + '</span></div>' +
      '</div>' +
      '<div class="agent-side">' +
        '<span>' + escapeHTML(sideTime) + '</span>' +
        (calls ? '<span class="tool-count">⌘ ' + calls + '</span>' : '<span></span>') +
      '</div>' +
    '</div>';
  }

  function toolRow(event) {
    const status = String(event.status || "");
    const timestamp = event.timestamp || event.started_at || event.finished_at;
    const detail = [event.source, timeAgo(timestamp)].filter(Boolean).join(" · ");
    const site = event.browser_context && event.browser_context.site ? event.browser_context.site : "";
    const subtitle = [detail, site].filter(Boolean).join(" · ");
    const dotClass = status === "running" ? "running" : (status === "error" ? "error" : "");

    return '<div class="tool-row">' +
      '<span class="tool-glyph">▣</span>' +
      '<div class="tool-main">' +
        '<div class="tool-name">' + escapeHTML(event.tool || "Tool call") + '</div>' +
        '<div class="tool-detail">' + escapeHTML(subtitle) + '</div>' +
      '</div>' +
      '<div class="tool-side">' +
        '<span>' + escapeHTML(formatDuration(event.duration_ms)) + '</span>' +
        '<span class="result-dot ' + dotClass + '"></span>' +
      '</div>' +
    '</div>';
  }

  function sessionRow(session) {
    const active = session.activity_state === "working";
    const detail = [session.tool, session.lifecycle_state].filter(Boolean).join(" · ");
    return '<div class="session-row">' +
      '<div><div class="session-name">' + escapeHTML(session.label || ("Session " + (session.flow_number || ""))) + '</div>' +
      '<div class="session-detail">' + escapeHTML(detail || (active ? "Working" : "Idle")) + '</div></div>' +
      '<span class="' + (active ? "live-dot" : "result-dot") + '"></span>' +
    '</div>';
  }

  function showLocked(pairError) {
    byId("dashboard").classList.add("hidden");
    byId("pairing").classList.remove("hidden");
    byId("serverStateText").textContent = "Pairing required";
    byId("version").textContent = "";
    byId("connectorText").textContent = "Mobile";
    byId("pairError").classList.toggle("hidden", !pairError);
  }

  function showDashboard() {
    byId("pairing").classList.add("hidden");
    byId("dashboard").classList.remove("hidden");
    byId("pairError").classList.add("hidden");
  }

  async function refresh() {
    try {
      const [status, agents, sessions, activity] = await Promise.all([
        json("/mobile/api/status"),
        json("/mobile/api/agents"),
        json("/mobile/api/sessions"),
        json("/mobile/api/activity"),
      ]);

      showDashboard();
      byId("serverStateText").textContent = "Server running";
      byId("version").textContent = status.version ? ("v" + status.version) : "";
      byId("connectorText").textContent = connectorName(status.connector);
      byId("calls1h").textContent = status.calls_1h == null ? "0" : status.calls_1h;
      byId("successRate").textContent = formatRate(status.success_rate);
      byId("activeAgents").textContent = status.active_agents == null ? "0" : status.active_agents;

      const agentRows = (agents.agents || []).map(agentRow).join("");
      byId("agents").innerHTML = agentRows || '<div class="empty">No delegated agents yet.</div>';
      byId("agentMeta").textContent = agents.active_count
        ? (agents.active_count + " active")
        : ((agents.count || 0) ? "Recent" : "");

      const seen = new Set();
      const combinedActivity = [...(activity.active || []), ...(activity.events || [])]
        .filter((event) => {
          const key = event.event_id || (event.tool + ":" + event.timestamp);
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .slice(0, 8);
      byId("activity").innerHTML = combinedActivity.map(toolRow).join("") ||
        '<div class="empty">No tool activity in the last hour.</div>';

      byId("sessionCount").textContent = sessions.count || 0;
      byId("sessions").innerHTML = (sessions.sessions || []).map(sessionRow).join("") ||
        '<div class="empty">No visible sessions.</div>';
    } catch (error) {
      if (error.status === 401) {
        showLocked(false);
        if (state.timer) clearInterval(state.timer);
        state.timer = null;
        return;
      }
      byId("serverStateText").textContent = "Connection unavailable";
    }
  }

  async function boot() {
    const code = pairingCode();
    if (code) {
      submitPairing(code);
      return;
    }

    const url = new URL(location.href);
    const pairError = url.searchParams.get("pair_error");
    if (pairError) {
      history.replaceState(null, "", location.pathname);
      showLocked(true);
      return;
    }

    await refresh();
    if (!state.timer && !byId("dashboard").classList.contains("hidden")) {
      state.timer = setInterval(refresh, 4000);
    }
  }

  boot();
})();
