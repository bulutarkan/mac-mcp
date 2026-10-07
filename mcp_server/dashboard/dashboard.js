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
    token: "",
    streamAbort: null,
    streamRetry: null,
    restoreFocus: null,
    agents: [],
    globalAdmission: null,
    focusAgent: null,
    focusTeam: null,
    focusApplied: false,
    changeSets: [],
    selectedChangeSet: 0,
    changesRefreshTimer: null,
    transactions: [],
    transactionOffset: 0,
    transactionLimit: 5,
    transactionTotal: 0,
    pendingUndo: null,
    transactionMessage: "",
    transactionRefreshTimer: null,
  };

  const $ = (id) => document.getElementById(id);
  const els = {
    connection: $("connectionState"), version: $("versionLabel"), uptime: $("uptimeLabel"),
    activeNow: $("activeNow"), lastTool: $("lastTool"), lastLatency: $("lastLatency"), traceBars: $("traceBars"),
    calls: $("metricCalls"), success: $("metricSuccess"), errors: $("metricErrors"), average: $("metricAverage"), p95: $("metricP95"), window: $("metricWindow"),
    rows: $("eventRows"), empty: $("emptyState"), activeStrip: $("activeStrip"), activeStripCount: $("activeStripCount"), topTools: $("topTools"), sourceMix: $("sourceMix"), agentCount: $("agentCount"), agentList: $("agentList"),
    changeCount: $("changeCount"), changeTaskSwitch: $("changeTaskSwitch"), changeHeadline: $("changeHeadline"), changeList: $("changeList"),
    transactionCount: $("transactionCount"), transactionMessage: $("transactionMessage"), transactionList: $("transactionList"),
    transactionNewer: $("transactionNewer"), transactionOlder: $("transactionOlder"), transactionPage: $("transactionPage"),
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

  const AUTH_STORAGE_KEY = "mac_mcp_dashboard_token";
  function bootstrapAuthToken() {
    const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const fragmentToken = fragment.get("token");
    if (fragmentToken) {
      window.sessionStorage.setItem(AUTH_STORAGE_KEY, fragmentToken);
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    return window.sessionStorage.getItem(AUTH_STORAGE_KEY) || "";
  }
  function authHeaders() {
    return state.token ? {Authorization: `Bearer ${state.token}`} : {};
  }
  state.token = bootstrapAuthToken();
  const initialQuery = new URLSearchParams(window.location.search);
  state.focusAgent = initialQuery.get("focus_agent");
  state.focusTeam = initialQuery.get("focus_team");

  async function fetchJSON(url) {
    const response = await fetch(url, {cache: "no-store", headers: authHeaders()});
    if (response.status === 401) { markAuthRequired(); throw new Error("401 Unauthorized"); }
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: {...authHeaders(), "Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    if (response.status === 401) { markAuthRequired(); throw new Error("401 Unauthorized"); }
    let data = {};
    try { data = await response.json(); } catch {}
    if (!response.ok) {
      const error = new Error(String(data.error || (response.status + " " + response.statusText)));
      error.code = data.error || "request_failed";
      throw error;
    }
    return data;
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
      els.agentCount.textContent = `${number(data.active_agents || 0)} active`;
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
      state.agents = data.agents || [];
      state.globalAdmission = data.global_admission || null;
      renderAgents(state.agents);
      if (state.changeSets.length) renderChanges();
    } catch (error) {
      els.agentList.innerHTML = `<div class="no-agents">Agent state is temporarily unavailable.</div>`;
    }
  }

  function changeSetLabel(changeSet, index) {
    const identity = changeSet.identity || {};
    if (identity.agent_id) {
      const agent = state.agents.find((item) => item.agent_id === identity.agent_id);
      if (agent?.title) return agent.title;
      return `Agent ${String(identity.agent_id).slice(-6)}`;
    }
    if (identity.team_id) return `Team ${String(identity.team_id).slice(-6)}`;
    if (identity.session_id) return index === 0 ? "Latest session" : `Session ${index + 1}`;
    return index === 0 ? "Latest task" : `Task ${index + 1}`;
  }

  function changeCategoryCode(category) {
    return ({files:"F", apps:"A", tabs:"B", commands:"T", external:"E", system:"S", agents:"AI"})[category] || "•";
  }

  function renderChanges() {
    const sets = state.changeSets || [];
    if (!sets.length) {
      els.changeCount.textContent = "0";
      els.changeTaskSwitch.innerHTML = "";
      els.changeHeadline.textContent = "No Mac changes recorded.";
      els.changeList.innerHTML = `<div class="no-agents">File, app, browser, command and external changes will appear here.</div>`;
      return;
    }
    state.selectedChangeSet = Math.max(0, Math.min(state.selectedChangeSet, sets.length - 1));
    const selected = sets[state.selectedChangeSet];
    els.changeCount.textContent = number(selected.change_count || 0);
    els.changeHeadline.textContent = selected.headline || "Mac changes recorded";
    els.changeTaskSwitch.innerHTML = sets.slice(0, 4).map((item, index) => `
      <button class="change-task-chip${index === state.selectedChangeSet ? " is-active" : ""}" type="button" data-change-index="${index}" title="${esc(changeSetLabel(item, index))}">${esc(changeSetLabel(item, index))}</button>`).join("");
    els.changeTaskSwitch.querySelectorAll("[data-change-index]").forEach((button) => button.addEventListener("click", () => {
      state.selectedChangeSet = Number(button.dataset.changeIndex || 0);
      renderChanges();
    }));
    const items = (selected.items || []).slice(0, 5);
    if (!items.length) {
      els.changeList.innerHTML = `<div class="no-agents">No side effects were recorded for this task.</div>`;
      return;
    }
    els.changeList.innerHTML = items.map((item) => {
      const detail = [item.target, item.detail].filter(Boolean).join(" · ");
      return `<div class="change-item">
        <span class="change-icon" aria-hidden="true">${esc(changeCategoryCode(item.category))}</span>
        <div class="change-copy"><strong>${esc(item.action || "Changed Mac state")}</strong><span title="${esc(detail)}">${esc(detail || item.tool || "macOS")}</span></div>
      </div>`;
    }).join("");
  }

  async function refreshChanges() {
    try {
      const data = await fetchJSON(`/dashboard/api/changes?hours=${encodeURIComponent(state.hours)}&limit=5&max_items=30`);
      state.changeSets = data.change_sets || [];
      state.selectedChangeSet = Math.min(state.selectedChangeSet, Math.max(0, state.changeSets.length - 1));
      renderChanges();
    } catch {
      els.changeHeadline.textContent = "Change receipts are temporarily unavailable.";
    }
  }

  function scheduleChangesRefresh() {
    window.clearTimeout(state.changesRefreshTimer);
    state.changesRefreshTimer = window.setTimeout(refreshChanges, 220);
  }

  function transactionOperationLabel(item) {
    const labels = {
      write_file: "Write file",
      write_files_batch: "Write files",
      edit_file: "Edit file",
      move_file: "Move file",
      copy_file: "Copy file",
      delete_path: "Delete path",
      create_directory: "Create directory",
      file_transaction_batch: "File batch",
      run_command: "Shell run",
      run_commands_parallel: "Parallel shell run",
      start_job: "Background job",
    };
    if (item.operation_class === "compound") return labels[item.operation] || "Compound change";
    return labels[item.operation] || (item.operation_class === "shell_capture" ? "Shell capture" : "Filesystem change");
  }

  function transactionStateLabel(item) {
    if (item.state === "undone") return "Undone";
    if (item.state === "rolled_back") return "Rolled back";
    if (item.state === "rollback_failed") return "Needs attention";
    if (item.reversibility === "partial") return "Partial";
    if (!item.undoable) return "Not reversible";
    if (item.can_undo) return "Undo available";
    return "Protected";
  }

  function undoBlockedLabel(reason) {
    return ({
      newer_change_conflict: "Changed later",
      already_undone: "Already undone",
      rolled_back: "Rolled back",
      rollback_failed: "Needs attention",
      expired: "Expired",
      irreversible: "Not reversible",
      not_committed: "Incomplete",
      preflight_failed: "Unavailable",
    })[reason] || "Undo unavailable";
  }

  function renderTransactions() {
    const items = state.transactions || [];
    els.transactionCount.textContent = number(state.transactionTotal || 0);
    els.transactionMessage.textContent = state.transactionMessage || "";
    const pageNumber = state.transactionTotal
      ? Math.floor(state.transactionOffset / state.transactionLimit) + 1
      : 0;
    const pageCount = state.transactionTotal
      ? Math.ceil(state.transactionTotal / state.transactionLimit)
      : 0;
    els.transactionPage.textContent = pageCount ? ("Page " + pageNumber + " / " + pageCount) : "—";
    els.transactionNewer.disabled = state.transactionOffset <= 0;
    els.transactionOlder.disabled = state.transactionOffset + items.length >= state.transactionTotal;

    if (!items.length) {
      els.transactionList.innerHTML = '<div class="no-agents">No transaction receipts in this page.</div>';
      return;
    }

    els.transactionList.innerHTML = items.map((item) => {
      const transactionId = String(item.transaction_id || "");
      const shortId = transactionId.startsWith("ftx_") ? transactionId.slice(0, 12) : transactionId.slice(0, 12);
      const status = transactionStateLabel(item);
      const reversibility = item.reversibility === "partial" ? "Partial undo coverage" : (item.undoable ? "Full undo coverage" : "No undo coverage");
      const unsupported = Number(item.unsupported_count || 0);
      let action = "";
      if (item.can_undo) {
        if (state.pendingUndo === transactionId) {
          action =
            '<div class="transaction-confirm">' +
              '<button class="transaction-btn is-confirm" type="button" data-confirm-undo="' + esc(transactionId) + '">Confirm</button>' +
              '<button class="transaction-btn" type="button" data-cancel-undo="' + esc(transactionId) + '">Cancel</button>' +
            '</div>';
        } else {
          action = '<button class="transaction-btn" type="button" data-undo-id="' + esc(transactionId) + '">' + (item.reversibility === "partial" ? "Undo files" : "Undo") + '</button>';
        }
      } else {
        action = '<span class="transaction-blocked">' + esc(undoBlockedLabel(item.undo_blocked_reason)) + '</span>';
      }
      return (
        '<div class="transaction-row" data-state="' + esc(item.state || "unknown") + '">' +
          '<div class="transaction-row-main">' +
            '<div class="transaction-row-title">' +
              '<strong>' + esc(transactionOperationLabel(item)) + '</strong>' +
              '<span class="transaction-time">' + esc(clock(item.committed_at || item.created_at)) + '</span>' +
            '</div>' +
            '<div class="transaction-meta">' +
              '<span>' + esc(item.target_summary || "Filesystem targets") + '</span>' +
              '<span>' + esc(reversibility) + '</span>' +
              (unsupported ? '<span>' + esc(unsupported + (unsupported === 1 ? " unsupported effect" : " unsupported effects")) + '</span>' : '') +
              '<span class="transaction-id">' + esc(shortId) + '</span>' +
            '</div>' +
          '</div>' +
          '<div class="transaction-row-action">' +
            '<span class="transaction-state">' + esc(status) + '</span>' +
            action +
          '</div>' +
        '</div>'
      );
    }).join("");

    els.transactionList.querySelectorAll("[data-undo-id]").forEach((button) => button.addEventListener("click", () => {
      state.pendingUndo = button.dataset.undoId;
      const selected = state.transactions.find((item) => item.transaction_id === state.pendingUndo);
      state.transactionMessage = selected?.reversibility === "partial"
        ? "Confirm Undo files: recorded filesystem changes will be restored; unsupported effects will remain."
        : "Confirm Undo to restore the recorded pre-change filesystem state.";
      renderTransactions();
    }));
    els.transactionList.querySelectorAll("[data-cancel-undo]").forEach((button) => button.addEventListener("click", () => {
      state.pendingUndo = null;
      state.transactionMessage = "";
      renderTransactions();
    }));
    els.transactionList.querySelectorAll("[data-confirm-undo]").forEach((button) => button.addEventListener("click", async () => {
      await performTransactionUndo(button.dataset.confirmUndo);
    }));
  }

  async function refreshTransactions() {
    try {
      const url = "/dashboard/api/transactions?limit=" + encodeURIComponent(state.transactionLimit) + "&offset=" + encodeURIComponent(state.transactionOffset);
      const data = await fetchJSON(url);
      state.transactionTotal = Number(data.total || 0);
      state.transactions = data.transactions || [];
      if (!state.transactions.length && state.transactionOffset > 0 && state.transactionTotal < state.transactionOffset) {
        state.transactionOffset = Math.max(0, Math.floor(Math.max(0, state.transactionTotal - 1) / state.transactionLimit) * state.transactionLimit);
        return refreshTransactions();
      }
      if (state.pendingUndo && !state.transactions.some((item) => item.transaction_id === state.pendingUndo && item.can_undo)) {
        state.pendingUndo = null;
      }
      renderTransactions();
    } catch {
      state.transactions = [];
      state.transactionMessage = "Transaction history is temporarily unavailable.";
      renderTransactions();
    }
  }

  function scheduleTransactionRefresh() {
    window.clearTimeout(state.transactionRefreshTimer);
    state.transactionRefreshTimer = window.setTimeout(refreshTransactions, 280);
  }

  function transactionJournalMayChange(tool) {
    return new Set([
      "write_file", "write_files_batch", "edit_file", "move_file", "copy_file",
      "delete_path", "create_directory", "file_transaction_batch", "file_transaction_undo",
      "run_command", "run_commands_parallel", "start_job", "get_job_status",
    ]).has(String(tool || ""));
  }

  async function performTransactionUndo(transactionId) {
    state.transactionMessage = "Undoing recorded filesystem changes…";
    renderTransactions();
    try {
      await postJSON("/dashboard/api/transactions/undo", {
        transaction_id: transactionId,
        confirm: true,
      });
      state.pendingUndo = null;
      await Promise.all([refreshTransactions(), refreshChanges()]);
      state.transactionMessage = "Undo completed safely.";
      renderTransactions();
    } catch (error) {
      state.pendingUndo = null;
      const messages = {
        transaction_conflict: "Undo blocked because the filesystem changed after this transaction.",
        transaction_expired: "Undo window expired.",
        transaction_irreversible: "This transaction is not reversible.",
        transaction_restore_failed: "Undo could not be completed. The transaction needs attention.",
        transaction_not_found: "Transaction receipt is no longer available.",
      };
      state.transactionMessage = messages[error.code] || "Undo could not be completed safely.";
      await refreshTransactions();
      renderTransactions();
    }
  }

  function renderTopTools(tools) {
    if (!tools.length) {
      els.topTools.innerHTML = `<div class="no-agents">Tool frequency will appear after calls are recorded.</div>`;
      return;
    }
    const max = Math.max(...tools.map((item) => Number(item.calls || 0)), 1);
    els.topTools.innerHTML = tools.slice(0, 4).map((item) => `
      <div class="top-tool">
        <strong title="${esc(item.tool)}">${esc(item.tool)}</strong>
        <span>${number(item.calls)} calls</span>
        <div class="tool-meter" aria-hidden="true"><i style="width:${Math.max(5, Number(item.calls || 0) / max * 100).toFixed(1)}%"></i></div>
      </div>`).join("");
  }

  function renderAgents(agents) {
    const admission = state.globalAdmission || {};
    const admissionReady = admission.global_limit !== undefined && admission.global_limit !== null;
    const scheduler = admissionReady ? `<div class="agent-group-label"><strong>Global scheduler</strong><span>${number(admission.global_active)}/${number(admission.global_limit)} active · ${number(admission.queued_count)} queued</span></div>` : '';
    if (!agents.length) {
      els.agentList.innerHTML = scheduler + `<div class="no-agents">No delegated agents yet. Enabled Subagent providers will appear here live.</div>`;
      return;
    }
    const activeStatuses = new Set(["starting", "running"]);
    const hiddenStatuses = new Set(["stalled"]);
    const matchesFocus = (agent) => Boolean(
      (state.focusAgent && agent.agent_id === state.focusAgent) ||
      (state.focusTeam && agent.team_id === state.focusTeam)
    );
    const focusedAgents = (state.focusAgent || state.focusTeam)
      ? agents.filter(matchesFocus).slice(0, 8)
      : [];
    const focusedIDs = new Set(focusedAgents.map((agent) => agent.agent_id));
    const active = agents
      .filter((agent) => activeStatuses.has(agent.status) && !focusedIDs.has(agent.agent_id))
      .slice(0, 4);
    const recent = agents
      .filter((agent) => !activeStatuses.has(agent.status) && !hiddenStatuses.has(agent.status) && !focusedIDs.has(agent.agent_id))
      .slice(0, Math.max(0, 8 - active.length));
    const card = (agent) => {
      const status = agent.status || "unknown";
      const phase = agent.phase || status;
      const model = [agent.provider, agent.model].filter(Boolean).join(" · ") || "Provider unavailable";
      const last = agent.last_tool ? `Last tool <b>${esc(agent.last_tool)}</b>` : `${number(agent.step_count)} steps`;
      const turn = Number(agent.turn_elapsed_ms || 0) > 0 ? ` · turn ${duration(agent.turn_elapsed_ms)}` : '';
      const resilience = [
        Number(agent.checkpoint_count || 0) > 0 ? `${number(agent.checkpoint_count)} checkpoints` : '',
        Number(agent.throttle_count || 0) > 0 ? `${number(agent.throttle_count)} throttles` : ''
      ].filter(Boolean).join(' · ');
      const avatar = String(agent.provider || "AI").slice(0, 2);
      const focused = Boolean(
        (state.focusAgent && agent.agent_id === state.focusAgent) ||
        (state.focusTeam && agent.team_id === state.focusTeam)
      );
      return `<div class="agent-card${focused ? " is-focus" : ""}" data-status="${esc(status)}" data-agent-id="${esc(agent.agent_id || "")}" data-team-id="${esc(agent.team_id || "")}"${focused ? ' tabindex="-1"' : ""}>
        <div class="agent-avatar" aria-hidden="true">${esc(avatar)}</div>
        <div class="agent-content">
          <div class="agent-card-head">
            <strong title="${esc(agent.title || agent.agent_id)}">${esc(agent.title || agent.agent_id)}</strong>
            <span class="agent-phase">${esc(phase)}</span>
          </div>
          <div class="agent-meta">${esc(model)}<br>${last} · ${number(agent.tool_call_count)} calls · ${duration(agent.duration_ms)}${turn}${resilience ? `<br>${esc(resilience)}` : ''}</div>
        </div>
      </div>`;
    };
    const groups = [];
    if (scheduler) groups.push(scheduler);
    if (focusedAgents.length) groups.push(`<div class="agent-group-label"><strong>Notification</strong><span>${focusedAgents.length}</span></div>${focusedAgents.map(card).join("")}`);
    if (active.length) groups.push(`<div class="agent-group-label"><strong>Active</strong><span>${active.length}</span></div>${active.map(card).join("")}`);
    if (recent.length) groups.push(`<div class="agent-group-label"><strong>Recent</strong><span>latest ${recent.length}</span></div>${recent.map(card).join("")}`);
    els.agentList.innerHTML = groups.join("");
    if (!state.focusApplied && (state.focusAgent || state.focusTeam)) {
      const focused = els.agentList.querySelector(".agent-card.is-focus");
      if (focused) {
        state.focusApplied = true;
        window.requestAnimationFrame(() => {
          focused.scrollIntoView({block: "center", behavior: "smooth"});
          focused.focus({preventScroll: true});
        });
      }
    }
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
      const h = item.duration ? Math.max(4, Math.min(48, item.duration / max * 48)) : 2;
      const x = index * width + 1;
      const y = 58 - h;
      const recent = index >= data.length - 3 ? " recent" : "";
      const error = item.error ? " error" : "";
      return `<rect class="trace-bar${recent}${error}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(2, width - 3).toFixed(1)}" height="${h.toFixed(1)}" rx="1"/>`;
    }).join("");
  }

  function renderActiveStrip() {
    const calls = Array.from(state.active.values())
      .sort((a, b) => Number(b.started_at || b.timestamp) - Number(a.started_at || a.timestamp))
      .slice(0, 6);
    const total = state.active.size;
    els.activeStripCount.textContent = `${total} running`;
    if (!calls.length) {
      els.activeStrip.innerHTML = `<div class="active-empty">No tools are running right now.</div>`;
      return;
    }
    const now = Date.now() / 1000;
    els.activeStrip.innerHTML = calls.map((event) => {
      const started = Number(event.started_at || event.timestamp || now);
      const elapsed = Math.max(0, (now - started) * 1000);
      return `<button class="active-call" type="button" data-event-id="${esc(event.event_id)}" aria-label="Inspect running ${esc(event.tool)} call">
        <strong>${esc(event.tool || "Tool call")}</strong>
        <small>${esc(compactToolDetail(event))}</small>
        <time>${duration(elapsed)}</time>
      </button>`;
    }).join("");
    els.activeStrip.querySelectorAll(".active-call").forEach((item) => item.addEventListener("click", () => openDrawer(item.dataset.eventId)));
  }

  function renderActiveCount() {
    const count = state.active.size;
    els.activeNow.textContent = `${count} active`;
    renderActiveStrip();
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
      scheduleChangesRefresh();
      if (transactionJournalMayChange(event.tool)) scheduleTransactionRefresh();
    }
  }

  function scheduleStreamReconnect() {
    window.clearTimeout(state.streamRetry);
    state.streamRetry = window.setTimeout(connectStream, 2000);
  }

  function handleSSEBlock(block) {
    let eventName = "message";
    const data = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (eventName !== "telemetry" || !data.length) return;
    try { handleTelemetry(JSON.parse(data.join("\n"))); } catch { /* malformed event is ignored */ }
  }

  async function connectStream() {
    if (state.streamAbort) state.streamAbort.abort();
    window.clearTimeout(state.streamRetry);
    const controller = new AbortController();
    state.streamAbort = controller;
    try {
      const response = await fetch("/dashboard/events", {
        cache: "no-store", headers: authHeaders(), signal: controller.signal,
      });
      if (response.status === 401) { markAuthRequired(); return; }
      if (!response.ok || !response.body) throw new Error(`${response.status} ${response.statusText}`);
      markOnline();
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (!controller.signal.aborted) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true}).replace(/\r\n/g, "\n");
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          handleSSEBlock(buffer.slice(0, boundary));
          buffer = buffer.slice(boundary + 2);
        }
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      markOffline(error);
    } finally {
      if (state.streamAbort === controller && !controller.signal.aborted) scheduleStreamReconnect();
    }
  }

  function markOnline() {
    els.connection.classList.remove("is-offline");
    els.connection.lastChild.textContent = "Live";
  }
  function markOffline() {
    els.connection.classList.add("is-offline");
    els.connection.lastChild.textContent = "Reconnecting";
  }
  function markAuthRequired() {
    els.connection.classList.add("is-offline");
    els.connection.lastChild.textContent = "Authentication required";
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
    await Promise.all([refreshSummary(), refreshEvents(), refreshChanges()]);
  }));
  els.sourceFilter.addEventListener("change", () => { state.source = els.sourceFilter.value; refreshEvents(); });
  els.statusFilter.addEventListener("change", () => { state.status = els.statusFilter.value; refreshEvents(); });
  let searchTimer;
  els.toolFilter.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => { state.tool = els.toolFilter.value.trim(); refreshEvents(); }, 180);
  });
  els.transactionNewer.addEventListener("click", () => {
    state.transactionOffset = Math.max(0, state.transactionOffset - state.transactionLimit);
    state.pendingUndo = null;
    state.transactionMessage = "";
    refreshTransactions();
  });
  els.transactionOlder.addEventListener("click", () => {
    if (state.transactionOffset + state.transactions.length >= state.transactionTotal) return;
    state.transactionOffset += state.transactionLimit;
    state.pendingUndo = null;
    state.transactionMessage = "";
    refreshTransactions();
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

  Promise.all([refreshSummary(), refreshEvents(), refreshAgents(), refreshChanges(), refreshTransactions()]).finally(connectStream);
  window.setInterval(refreshSummary, 5000);
  window.setInterval(refreshAgents, 2200);
  window.setInterval(() => { if (!document.hidden) refreshChanges(); }, 10000);
  window.setInterval(() => { if (!document.hidden) refreshTransactions(); }, 10000);
  window.setInterval(() => { if (state.active.size) renderActiveStrip(); }, 1000);
  window.setInterval(() => { if (!document.hidden) refreshEvents(); }, 20000);
})();
