(() => {
  "use strict";

  const state = {
    hours: 24,
    source: "all",
    status: "all",
    tool: "",
    events: [],
    active: new Map(),
    trace: [],
    selected: null,
    eventSource: null,
    restoreFocus: null,
  };

  const $ = (id) => document.getElementById(id);
  const els = {
    connection: $("connectionState"), version: $("versionLabel"), uptime: $("uptimeLabel"),
    activeNow: $("activeNow"), lastTool: $("lastTool"), lastLatency: $("lastLatency"), traceBars: $("traceBars"),
    calls: $("metricCalls"), success: $("metricSuccess"), errors: $("metricErrors"), average: $("metricAverage"), p95: $("metricP95"), window: $("metricWindow"),
    rows: $("eventRows"), empty: $("emptyState"), topTools: $("topTools"), sourceMix: $("sourceMix"), agentCount: $("agentCount"), agentList: $("agentList"),
    toolFilter: $("toolFilter"), sourceFilter: $("sourceFilter"), statusFilter: $("statusFilter"),
    drawer: $("detailDrawer"), backdrop: $("drawerBackdrop"), drawerClose: $("drawerClose"), drawerStatus: $("drawerStatus"), drawerTitle: $("drawerTitle"), drawerMeta: $("drawerMeta"),
    drawerRequest: $("drawerRequest"), drawerResult: $("drawerResult"), drawerError: $("drawerError"), resultSize: $("resultSize"), resultSection: $("resultSection"), errorSection: $("errorSection"),
  };

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
  const pretty = (value) => {
    if (value === undefined || value === null) return "—";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch { return String(value); }
  };
  const number = (value) => new Intl.NumberFormat("en-US").format(Number(value || 0));
  const duration = (ms) => {
    const n = Number(ms || 0);
    if (n < 1000) return `${Math.round(n)} ms`;
    if (n < 60000) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)} s`;
    return `${Math.floor(n / 60000)}m ${Math.round((n % 60000) / 1000)}s`;
  };
  const bytes = (n) => {
    const value = Number(n || 0);
    if (!value) return "0 B";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  };
  const clock = (ts) => {
    if (!ts) return "—";
    return new Date(Number(ts) * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  };
  const compactToolDetail = (event) => {
    const args = event.arguments || {};
    for (const key of ["path", "url", "command", "query", "app", "browser", "title", "pattern", "cwd"]) {
      const value = args[key];
      if (typeof value === "string" && value.trim()) return value.replace(/\s+/g, " ").slice(0, 90);
    }
    return event.source === "rest" ? "Legacy REST surface" : "MCP tool call";
  };
  const windowLabel = () => state.hours === 1 ? "last hour" : state.hours === 24 ? "last 24 hours" : state.hours === 168 ? "last 7 days" : `last ${state.hours} hours`;

  async function fetchJSON(url) {
    const response = await fetch(url, {cache: "no-store"});
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  }

  async function refreshSummary() {
    try {
      const data = await fetchJSON(`/dashboard/api/summary?hours=${encodeURIComponent(state.hours)}`);
      els.calls.textContent = number(data.total_calls);
      els.success.textContent = `${Number(data.success_rate || 0).toFixed(1).replace(".0", "")}%`;
      els.errors.textContent = `${number(data.error_calls)} ${data.error_calls === 1 ? "error" : "errors"}`;
      els.average.textContent = duration(data.avg_duration_ms);
      els.p95.textContent = duration(data.p95_duration_ms);
      els.activeNow.textContent = `${number(data.active_calls)} active`;
      els.window.textContent = windowLabel();
      els.version.textContent = `v${data.version || "—"}`;
      els.uptime.textContent = `Uptime ${formatUptime(data.uptime_seconds)}`;
      renderTopTools(data.top_tools || []);
      const mix = (data.sources || []).map((item) => `${String(item.source).toUpperCase()} ${item.calls}`).join(" / ");
      els.sourceMix.textContent = mix || "No calls";
      els.agentCount.textContent = data.active_agents ? `${data.active_agents} active` : `${data.agent_count || 0}`;
    } catch (error) {
      markOffline(error);
    }
  }

  async function refreshEvents() {
    const params = new URLSearchParams({hours: String(state.hours), limit: "160"});
    if (state.source !== "all") params.set("source", state.source);
    if (state.status !== "all") params.set("status", state.status);
    if (state.tool) params.set("tool", state.tool);
    try {
      const data = await fetchJSON(`/dashboard/api/events?${params}`);
      state.events = data.events || [];
      state.active = new Map((data.active || []).map((event) => [event.event_id, event]));
      seedTrace(state.events);
      renderEvents();
      renderActiveCount();
    } catch (error) {
      markOffline(error);
    }
  }

  async function refreshAgents() {
    try {
      const data = await fetchJSON("/dashboard/api/agents?limit=16");
      renderAgents(data.agents || []);
    } catch (error) {
      els.agentList.innerHTML = `<div class="no-agents">Agent state is temporarily unavailable.</div>`;
    }
  }

  function renderTopTools(tools) {
    if (!tools.length) {
      els.topTools.innerHTML = `<div class="no-agents">Tool frequency will appear after calls are recorded.</div>`;
      return;
    }
    const max = Math.max(...tools.map((item) => Number(item.calls || 0)), 1);
    els.topTools.innerHTML = tools.map((item) => `
      <div class="top-tool">
        <strong title="${esc(item.tool)}">${esc(item.tool)}</strong>
        <span>${number(item.calls)} calls</span>
        <div class="tool-meter" aria-hidden="true"><i style="width:${Math.max(5, Number(item.calls || 0) / max * 100).toFixed(1)}%"></i></div>
      </div>`).join("");
  }

  function renderAgents(agents) {
    if (!agents.length) {
      els.agentList.innerHTML = `<div class="no-agents">No delegated agents yet. Spawned OpenCode or Codex workers will appear here live.</div>`;
      return;
    }
    els.agentList.innerHTML = agents.slice(0, 8).map((agent) => {
      const status = agent.status || "unknown";
      const phase = agent.phase || status;
      const model = [agent.provider, agent.model].filter(Boolean).join(" · ") || "Provider unavailable";
      const last = agent.last_tool ? `Last tool <b>${esc(agent.last_tool)}</b>` : `${number(agent.step_count)} steps`;
      return `<div class="agent-card" data-status="${esc(status)}">
        <div class="agent-card-head">
          <strong title="${esc(agent.title || agent.agent_id)}">${esc(agent.title || agent.agent_id)}</strong>
          <span class="agent-phase">${esc(phase)}</span>
        </div>
        <div class="agent-meta">${esc(model)}<br>${last} · ${number(agent.tool_call_count)} calls · ${duration(agent.duration_ms)}</div>
      </div>`;
    }).join("");
  }

  function visibleEvents() {
    const active = Array.from(state.active.values()).filter(matchesFilters);
    const finished = state.events.filter(matchesFilters);
    return [...active, ...finished]
      .sort((a, b) => Number(b.started_at || b.timestamp) - Number(a.started_at || a.timestamp))
      .slice(0, 180);
  }

  function matchesFilters(event) {
    if (state.source !== "all" && event.source !== state.source) return false;
    if (state.status !== "all" && event.status !== state.status) return false;
    if (state.tool && !String(event.tool || "").toLowerCase().includes(state.tool.toLowerCase())) return false;
    return true;
  }

  function renderEvents(newEventId = null) {
    const events = visibleEvents();
    els.empty.hidden = events.length > 0;
    els.rows.innerHTML = events.map((event) => {
      const status = event.status || "running";
      return `<button class="event-row${event.event_id === newEventId ? " is-new" : ""}" type="button" role="row" data-event-id="${esc(event.event_id)}" aria-label="Inspect ${esc(event.tool)} call, ${esc(status)}">
        <span class="event-time" role="cell">${clock(event.started_at || event.timestamp)}</span>
        <span class="event-tool" role="cell"><strong>${esc(event.tool)}</strong><small>${esc(compactToolDetail(event))}</small></span>
        <span class="source-chip" role="cell">${esc(event.source || "mcp")}</span>
        <span class="duration" role="cell">${event.status === "running" ? "live" : duration(event.duration_ms)}</span>
        <span class="status status-${esc(status)}" role="cell">${status === "success" ? "Success" : status === "error" ? "Error" : "Running"}</span>
      </button>`;
    }).join("");
    els.rows.querySelectorAll(".event-row").forEach((row) => row.addEventListener("click", () => openDrawer(row.dataset.eventId)));
  }

  function openDrawer(eventId) {
    const event = state.active.get(eventId) || state.events.find((item) => item.event_id === eventId);
    if (!event) return;
    state.selected = event;
    state.restoreFocus = document.activeElement;
    const status = event.status || "running";
    els.drawerStatus.className = `status-badge ${status === "error" ? "error" : status === "running" ? "running" : ""}`;
    els.drawerStatus.textContent = status === "success" ? "Success" : status === "error" ? "Error" : "Running";
    els.drawerTitle.textContent = event.tool || "Tool call";
    els.drawerMeta.textContent = `${String(event.source || "mcp").toUpperCase()} · ${clock(event.started_at)} · ${event.status === "running" ? "in progress" : duration(event.duration_ms)}`;
    els.drawerRequest.textContent = pretty(event.arguments || {});
    els.drawerResult.textContent = pretty(event.result);
    els.resultSize.textContent = bytes(event.result_size);
    els.resultSection.hidden = status === "running" && event.result == null;
    els.errorSection.hidden = !event.error;
    els.drawerError.textContent = event.error || "";
    els.backdrop.hidden = false;
    els.backdrop.classList.add("is-open");
    els.drawer.classList.add("is-open");
    els.drawer.setAttribute("aria-hidden", "false");
    els.drawerClose.focus();
  }

  function closeDrawer() {
    els.backdrop.classList.remove("is-open");
    els.drawer.classList.remove("is-open");
    els.drawer.setAttribute("aria-hidden", "true");
    window.setTimeout(() => { els.backdrop.hidden = true; }, 240);
    if (state.restoreFocus && typeof state.restoreFocus.focus === "function") state.restoreFocus.focus();
    state.selected = null;
  }

  function seedTrace(events) {
    state.trace = events.slice(0, 40).reverse().map((event) => ({duration: Number(event.duration_ms || 0), error: event.status === "error"}));
    renderTrace();
  }

  function pushTrace(event) {
    if (event.status === "running") return;
    state.trace.push({duration: Number(event.duration_ms || 0), error: event.status === "error"});
    state.trace = state.trace.slice(-40);
    renderTrace();
  }

  function renderTrace() {
    const data = state.trace.length ? state.trace : Array.from({length: 22}, () => ({duration: 0, error: false}));
    const max = Math.max(250, ...data.map((item) => item.duration));
    const width = 560 / Math.max(data.length, 1);
    els.traceBars.innerHTML = data.map((item, index) => {
      const h = item.duration ? Math.max(5, Math.min(64, item.duration / max * 64)) : 2;
      const x = index * width + 1;
      const y = 72 - h;
      const recent = index >= data.length - 3 ? " recent" : "";
      const error = item.error ? " error" : "";
      return `<rect class="trace-bar${recent}${error}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(2, width - 3).toFixed(1)}" height="${h.toFixed(1)}" rx="1"/>`;
    }).join("");
  }

  function renderActiveCount() {
    const count = state.active.size;
    els.activeNow.textContent = `${count} active`;
  }

  function handleTelemetry(event) {
    if (!event || !event.kind) return;
    markOnline();
    if (event.kind === "connected") {
      state.active = new Map((event.active || []).map((item) => [item.event_id, item]));
      renderEvents();
      renderActiveCount();
      return;
    }
    if (event.kind === "call_started") {
      state.active.set(event.event_id, event);
      els.lastTool.textContent = event.tool || "Tool call";
      els.lastLatency.textContent = "running";
      renderEvents(event.event_id);
      renderActiveCount();
      return;
    }
    if (event.kind === "call_finished") {
      state.active.delete(event.event_id);
      state.events = [event, ...state.events.filter((item) => item.event_id !== event.event_id)].slice(0, 220);
      els.lastTool.textContent = event.tool || "Tool call";
      els.lastLatency.textContent = duration(event.duration_ms);
      pushTrace(event);
      renderEvents(event.event_id);
      renderActiveCount();
      refreshSummary();
    }
  }

  function connectStream() {
    if (state.eventSource) state.eventSource.close();
    const stream = new EventSource("/dashboard/events");
    state.eventSource = stream;
    stream.addEventListener("telemetry", (message) => {
      try { handleTelemetry(JSON.parse(message.data)); } catch { /* malformed event is ignored */ }
    });
    stream.onopen = markOnline;
    stream.onerror = () => {
      markOffline();
      // EventSource reconnects automatically. Keep the UI useful while it does.
    };
  }

  function markOnline() {
    els.connection.classList.remove("is-offline");
    els.connection.lastChild.textContent = "Live";
  }
  function markOffline() {
    els.connection.classList.add("is-offline");
    els.connection.lastChild.textContent = "Reconnecting";
  }
  function formatUptime(seconds) {
    const s = Number(seconds || 0);
    const days = Math.floor(s / 86400);
    const hours = Math.floor((s % 86400) / 3600);
    const mins = Math.floor((s % 3600) / 60);
    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${mins}m`;
    return `${mins}m`;
  }

  document.querySelectorAll("[data-hours]").forEach((button) => button.addEventListener("click", async () => {
    state.hours = Number(button.dataset.hours);
    document.querySelectorAll("[data-hours]").forEach((item) => item.classList.toggle("is-active", item === button));
    await Promise.all([refreshSummary(), refreshEvents()]);
  }));
  els.sourceFilter.addEventListener("change", () => { state.source = els.sourceFilter.value; refreshEvents(); });
  els.statusFilter.addEventListener("change", () => { state.status = els.statusFilter.value; refreshEvents(); });
  let searchTimer;
  els.toolFilter.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => { state.tool = els.toolFilter.value.trim(); refreshEvents(); }, 180);
  });
  els.drawerClose.addEventListener("click", closeDrawer);
  els.backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && els.drawer.classList.contains("is-open")) closeDrawer();
    if (event.key === "Tab" && els.drawer.classList.contains("is-open")) {
      const focusable = Array.from(els.drawer.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")).filter((el) => !el.disabled);
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });

  Promise.all([refreshSummary(), refreshEvents(), refreshAgents()]).finally(connectStream);
  window.setInterval(refreshSummary, 5000);
  window.setInterval(refreshAgents, 2200);
  window.setInterval(() => { if (!document.hidden) refreshEvents(); }, 20000);
})();
