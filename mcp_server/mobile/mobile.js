(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const state = { timer: null };

  function deviceName() {
    const ua = navigator.userAgent || "";
    if (/iPad/i.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "iPad";
    if (/iPhone/i.test(ua)) return "iPhone";
    return "Mobile device";
  }

  async function json(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.error || ("HTTP " + response.status));
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function pairCodeFromFragment() {
    const hash = location.hash.startsWith("#") ? location.hash.slice(1) : "";
    const params = new URLSearchParams(hash);
    const code = params.get("pair");
    if (code) history.replaceState(null, "", location.pathname + location.search);
    return code;
  }

  async function pair(code) {
    if (!code) return false;
    try {
      await json("/mobile/pair", {
        method: "POST",
        body: JSON.stringify({ code: code, device_name: deviceName() }),
      });
      return true;
    } catch (error) {
      byId("pairError").textContent = "This pairing code is invalid, expired, or already used.";
      byId("pairError").classList.remove("hidden");
      return false;
    }
  }

  function ago(seconds) {
    if (seconds == null) return "";
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    return Math.floor(s / 3600) + "h";
  }

  function escapeHTML(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
  }

  function row(primary, secondary, active, time, warn) {
    return '<div class="row">' +
      '<span class="dot ' + (active ? "active" : "") + ' ' + (warn ? "warn" : "") + '"></span>' +
      '<div><div class="primary">' + escapeHTML(primary) + '</div><div class="secondary">' + escapeHTML(secondary || "") + '</div></div>' +
      '<div class="time">' + escapeHTML(time || "") + '</div>' +
      '</div>';
  }

  function showLocked() {
    byId("dashboard").classList.add("hidden");
    byId("pairing").classList.remove("hidden");
    byId("connection").textContent = "Pairing required";
    byId("connection").classList.remove("online");
  }

  function showDashboard() {
    byId("pairing").classList.add("hidden");
    byId("dashboard").classList.remove("hidden");
    byId("connection").textContent = "Online";
    byId("connection").classList.add("online");
  }

  async function refresh() {
    try {
      const data = await Promise.all([
        json("/mobile/api/status"),
        json("/mobile/api/agents"),
        json("/mobile/api/sessions"),
        json("/mobile/api/activity"),
      ]);
      const status = data[0];
      const agents = data[1];
      const sessions = data[2];
      const activity = data[3];

      showDashboard();
      byId("activeAgents").textContent = status.active_agents == null ? 0 : status.active_agents;
      byId("agentCount").textContent = status.agent_count == null ? 0 : status.agent_count;
      byId("version").textContent = status.version ? ("v" + status.version) : "";

      const agentRows = (agents.agents || []).map((agent) => {
        const active = ["starting", "running"].includes(agent.status);
        const label = agent.title || agent.team_task_id || ((agent.provider || "Agent") + " " + (agent.model || "")).trim();
        const detail = [agent.provider, agent.model, agent.phase || agent.status, agent.last_tool].filter(Boolean).join(" · ");
        const duration = agent.duration_ms != null ? ago(agent.duration_ms / 1000) : "";
        return row(label, detail, active, duration, agent.status === "failed");
      }).join("");
      byId("agents").innerHTML = agentRows || '<div class="empty">No delegated agents yet.</div>';

      byId("sessionCount").textContent = (sessions.count || 0) + " visible";
      const sessionRows = (sessions.sessions || []).map((session) => {
        const active = session.activity_state === "working";
        const detail = [session.tool, session.lifecycle_state].filter(Boolean).join(" · ");
        const warn = ["failed", "disconnected", "expired"].includes(session.lifecycle_state);
        return row(session.label || ("Session " + (session.flow_number || "")), detail, active, ago((session.activity_ms || 0) / 1000), warn);
      }).join("");
      byId("sessions").innerHTML = sessionRows || '<div class="empty">No active or retained sessions.</div>';

      const activityRows = (activity.events || []).slice(0, 12).map((event) => {
        const site = event.browser_context && event.browser_context.site ? event.browser_context.site : "";
        const detail = [event.source, site].filter(Boolean).join(" · ");
        const duration = event.duration_ms != null ? (Math.round(event.duration_ms) + "ms") : "";
        return row(event.tool || "Tool call", detail, event.status === "running", duration, event.status === "error");
      }).join("");
      byId("activity").innerHTML = activityRows || '<div class="empty">No recent activity.</div>';
    } catch (error) {
      if (error.status === 401) {
        showLocked();
        if (state.timer) clearInterval(state.timer);
        state.timer = null;
        return;
      }
      byId("connection").textContent = "Offline";
      byId("connection").classList.remove("online");
    }
  }

  async function boot() {
    const code = pairCodeFromFragment();
    if (code) await pair(code);
    await refresh();
    if (!state.timer) state.timer = setInterval(refresh, 4000);
  }

  boot();
})();
