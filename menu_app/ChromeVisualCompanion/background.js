'use strict';

try { importScripts('bridge_config.js'); } catch (_) {}

const CONFIG = globalThis.MAC_MCP_CHROME_BRIDGE || {};
const PORT = Number(CONFIG.port || 0);
const TOKEN = String(CONFIG.token || '');
const RECONNECT_MS = Math.max(250, Math.min(Number(CONFIG.reconnect_ms || 1000), 10000));
// Capabilities the server may rely on; an older companion simply does not list them.
const FEATURES = ['dialogs', 'gestures', 'alarm_reconnect', 'tab_queue', 'dialog_memory'];
const MAX_GESTURE_STEPS = 80;
const DIALOG_TEXT_LIMIT = 300;
let socket = null;
let reconnectTimer = null;
let pingTimer = null;

function clearTimers() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (pingTimer) clearInterval(pingTimer);
  pingTimer = null;
}

function scheduleReconnect() {
  if (!PORT || !TOKEN || reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_MS);
}

function send(payload) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(payload));
  return true;
}

async function handleOpenTab(message) {
  const requestId = String(message.request_id || '');
  const url = String(message.url || '');
  if (!requestId || !/^https?:\/\//i.test(url)) {
    send({type: 'result', request_id: requestId, ok: false, error: 'invalid_request'});
    return;
  }
  try {
    let win = null;
    try {
      win = await chrome.windows.getLastFocused({windowTypes: ['normal']});
    } catch (_) {}
    if (!win || !Number.isInteger(win.id) || win.id < 0) {
      send({type: 'result', request_id: requestId, ok: false, error: 'no_normal_chrome_window', message: 'Chrome has no normal window available for background work.'});
      return;
    }
    const tab = await chrome.tabs.create({url, active: false, windowId: win.id});
    if (tab.active === true) {
      try { await chrome.tabs.remove(tab.id); } catch (_) {}
      send({type: 'result', request_id: requestId, ok: false, error: 'chrome_created_active_tab', message: 'Chrome unexpectedly activated the requested background tab.'});
      return;
    }
    send({
      type: 'result', request_id: requestId, ok: true,
      chrome_tab_id: tab.id, chrome_window_id: tab.windowId,
      index: tab.index, active: false, url: tab.url || url
    });
  } catch (error) {
    send({
      type: 'result', request_id: requestId, ok: false,
      error: 'chrome_tabs_create_failed', message: String(error && error.message || error || 'unknown')
    });
  }
}


// Tabs this companion currently holds a debugger session on.
const ourSessions = new Set();
// One operation per tab at a time: a new request waits until the previous one has detached,
// so it never meets this companion's own session ("Another debugger is already attached").
const tabQueues = new Map();

function withTab(tabId, work) {
  const previous = tabQueues.get(tabId) || Promise.resolve();
  const run = previous.catch(() => {}).then(work);
  const tail = run.catch(() => {});
  tabQueues.set(tabId, tail);
  tail.then(() => { if (tabQueues.get(tabId) === tail) tabQueues.delete(tabId); });
  return run;
}

function debuggerAttach(target) {
  return new Promise((resolve, reject) => {
    chrome.debugger.attach(target, '1.3', () => {
      const err = chrome.runtime.lastError;
      if (!err) { ourSessions.add(target.tabId); resolve(); return; }
      const text = String(err.message || 'debugger_attach_failed');
      // Our own session survived an earlier call (e.g. a detach that a dialog held up): reuse it.
      if (/already attached/i.test(text) && ourSessions.has(target.tabId)) resolve();
      else reject(new Error(text));
    });
  });
}

function debuggerDetach(target) {
  return new Promise((resolve) => {
    // A tab showing a native dialog can hold a detach; never let that block the next request.
    const timer = setTimeout(resolve, 1500);
    chrome.debugger.detach(target, () => {
      void chrome.runtime.lastError;
      ourSessions.delete(target.tabId);
      clearTimeout(timer);
      resolve();
    });
  });
}

chrome.debugger.onDetach.addListener((source) => { if (source && source.tabId != null) ourSessions.delete(source.tabId); });

function debuggerCommand(target, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand(target, method, params || {}, (result) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message || 'debugger_command_failed'));
      else resolve(result || {});
    });
  });
}

// Dialogs seen and not yet answered, per tab. Chrome does not repeat the opening event to a
// new session, so later calls probe briefly instead of hanging behind the dialog.
const openDialogs = new Map();
const DIALOG_PROBE_MS = 700;

function enablePage(target) {
  // Bounded: a page held by a dialog must not stall the request before it can be reported.
  return Promise.race([
    debuggerCommand(target, 'Page.enable', {}).catch(() => {}),
    new Promise((resolve) => setTimeout(resolve, 500))
  ]);
}

function dialogWatcher(tabId) {
  // Resolves when the page shows (or already shows) a native alert/confirm/prompt/beforeunload dialog.
  let listener = null;
  const promise = new Promise((resolve) => {
    listener = (source, method, params) => {
      if (source.tabId === tabId && method === 'Page.javascriptDialogOpening') resolve(params || {});
    };
    chrome.debugger.onEvent.addListener(listener);
  });
  return {promise, stop: () => { if (listener) chrome.debugger.onEvent.removeListener(listener); }};
}

function dialogResult(requestId, tabId, params) {
  return {
    type: 'result', request_id: requestId, ok: false, error: 'browser_dialog_open', chrome_tab_id: tabId,
    message: 'A native browser dialog is open on this tab; nothing else can run until it is answered.',
    dialog: {
      dialog_type: String(params.type || 'alert'),
      message: String(params.message || '').slice(0, DIALOG_TEXT_LIMIT),
      default_prompt: String(params.defaultPrompt || '').slice(0, DIALOG_TEXT_LIMIT),
      url: String(params.url || '').slice(0, 500)
    }
  };
}

async function handleExecuteJs(message) {
  const requestId = String(message.request_id || '');
  const tabId = Number(message.chrome_tab_id);
  const js = String(message.js || '');
  if (!requestId || !Number.isInteger(tabId) || tabId < 0 || !js || js.length > 8000000) {
    send({type: 'result', request_id: requestId, ok: false, error: 'invalid_execute_js_request'});
    return;
  }
  const target = {tabId};
  let attached = false;
  const watcher = dialogWatcher(tabId);
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab.active === true) { /* active is allowed; transport itself never activates it */ }
    await debuggerAttach(target);
    attached = true;
    // Page.enable also reports a dialog that was already open before we attached.
    await enablePage(target);
    const known = openDialogs.get(tabId);
    if (known) {
      // A trivial script answers at once unless the dialog still blocks the page.
      const probe = debuggerCommand(target, 'Runtime.evaluate', {expression: '1', returnByValue: true});
      const state = await Promise.race([
        probe.then(() => 'clear', () => 'clear'),
        watcher.promise.then(() => 'blocked'),
        new Promise((resolve) => setTimeout(() => resolve('blocked'), DIALOG_PROBE_MS))
      ]);
      if (state === 'blocked') { send(dialogResult(requestId, tabId, known)); return; }
      openDialogs.delete(tabId);
    }
    const evaluation = debuggerCommand(target, 'Runtime.evaluate', {
      expression: js, returnByValue: true, awaitPromise: true, userGesture: false
    }).then((out) => ({out}));
    const first = await Promise.race([evaluation, watcher.promise.then((dialog) => ({dialog}))]);
    if (first.dialog) {
      evaluation.catch(() => {});
      openDialogs.set(tabId, first.dialog);
      send(dialogResult(requestId, tabId, first.dialog));
      return;
    }
    const out = first.out;
    if (out.exceptionDetails) {
      const detail = out.exceptionDetails.exception && out.exceptionDetails.exception.description;
      throw new Error(detail || out.exceptionDetails.text || 'runtime_evaluate_failed');
    }
    const remote = out.result || {};
    let value = remote.value;
    if (value === undefined || value === null) value = '';
    else if (typeof value === 'object') value = JSON.stringify(value);
    else value = String(value);
    send({type: 'result', request_id: requestId, ok: true, result: value, chrome_tab_id: tabId});
  } catch (error) {
    send({type: 'result', request_id: requestId, ok: false, error: 'chrome_debugger_evaluate_failed', message: String(error && error.message || error || 'unknown')});
  } finally {
    watcher.stop();
    if (attached) { try { await debuggerDetach(target); } catch (_) {} }
  }
}

async function handleDialog(message) {
  // Answers an open dialog only on an explicit accept/dismiss decision from the caller.
  const requestId = String(message.request_id || '');
  const tabId = Number(message.chrome_tab_id);
  if (!requestId || !Number.isInteger(tabId) || tabId < 0 || typeof message.accept !== 'boolean') {
    send({type: 'result', request_id: requestId, ok: false, error: 'invalid_dialog_request'});
    return;
  }
  const target = {tabId};
  let attached = false;
  const watcher = dialogWatcher(tabId);
  try {
    await chrome.tabs.get(tabId);
    await debuggerAttach(target);
    attached = true;
    await enablePage(target);
    const seen = await Promise.race([watcher.promise, new Promise((resolve) => setTimeout(() => resolve(null), 400))]);
    const params = {accept: message.accept};
    if (typeof message.prompt_text === 'string') params.promptText = message.prompt_text.slice(0, 2000);
    await debuggerCommand(target, 'Page.handleJavaScriptDialog', params);
    const answered = seen || openDialogs.get(tabId) || null;
    openDialogs.delete(tabId);
    send({
      type: 'result', request_id: requestId, ok: true, chrome_tab_id: tabId, accepted: message.accept,
      dialog: answered ? {
        dialog_type: String(answered.type || answered.dialog_type || ''),
        message: String(answered.message || '').slice(0, DIALOG_TEXT_LIMIT)
      } : null
    });
  } catch (error) {
    const text = String(error && error.message || error || 'unknown');
    if (/no dialog/i.test(text)) openDialogs.delete(tabId);
    send({
      type: 'result', request_id: requestId, ok: false,
      error: /no dialog/i.test(text) ? 'no_dialog_open' : 'chrome_dialog_failed', message: text
    });
  } finally {
    watcher.stop();
    if (attached) { try { await debuggerDetach(target); } catch (_) {} }
  }
}

async function handleGesture(message) {
  // A trusted pointer sequence (hover, drag); the button is always released, even on failure.
  const requestId = String(message.request_id || '');
  const tabId = Number(message.chrome_tab_id);
  const steps = Array.isArray(message.steps) ? message.steps : [];
  const valid = steps.length > 0 && steps.length <= MAX_GESTURE_STEPS && steps.every((step) =>
    step && ['move', 'down', 'up'].includes(step.type) && Number.isFinite(Number(step.x)) && Number.isFinite(Number(step.y))
    && Number(step.x) >= 0 && Number(step.y) >= 0);
  if (!requestId || !Number.isInteger(tabId) || tabId < 0 || !valid) {
    send({type: 'result', request_id: requestId, ok: false, error: 'invalid_gesture_request'});
    return;
  }
  const target = {tabId};
  let attached = false;
  let pressed = null;
  let done = 0;
  try {
    await chrome.tabs.get(tabId);
    await debuggerAttach(target);
    attached = true;
    await debuggerCommand(target, 'Emulation.setFocusEmulationEnabled', {enabled: true});
    for (const step of steps) {
      const x = Number(step.x), y = Number(step.y);
      if (step.type === 'move') {
        await debuggerCommand(target, 'Input.dispatchMouseEvent', {
          type: 'mouseMoved', x, y, button: pressed ? 'left' : 'none', buttons: pressed ? 1 : 0, pointerType: 'mouse'
        });
      } else if (step.type === 'down') {
        await debuggerCommand(target, 'Input.dispatchMouseEvent', {
          type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1, pointerType: 'mouse'
        });
        pressed = {x, y};
      } else {
        await debuggerCommand(target, 'Input.dispatchMouseEvent', {
          type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1, pointerType: 'mouse'
        });
        pressed = null;
      }
      done += 1;
      const delay = Math.max(0, Math.min(Number(step.delay_ms || 0), 1000));
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    }
    send({type: 'result', request_id: requestId, ok: true, chrome_tab_id: tabId, steps_done: done});
  } catch (error) {
    send({
      type: 'result', request_id: requestId, ok: false, error: 'chrome_gesture_failed', steps_done: done,
      message: String(error && error.message || error || 'unknown')
    });
  } finally {
    if (attached) {
      if (pressed) {
        try {
          await debuggerCommand(target, 'Input.dispatchMouseEvent', {
            type: 'mouseReleased', x: pressed.x, y: pressed.y, button: 'left', buttons: 0, clickCount: 1, pointerType: 'mouse'
          });
        } catch (_) {}
      }
      try { await debuggerCommand(target, 'Emulation.setFocusEmulationEnabled', {enabled: false}); } catch (_) {}
      try { await debuggerDetach(target); } catch (_) {}
    }
  }
}


async function handleDispatchMouse(message) {
  const requestId = String(message.request_id || '');
  const tabId = Number(message.chrome_tab_id);
  const x = Number(message.x);
  const y = Number(message.y);
  const clickCount = Number(message.click_count) === 2 ? 2 : 1;
  if (!requestId || !Number.isInteger(tabId) || tabId < 0 || !Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0) {
    send({type: 'result', request_id: requestId, ok: false, error: 'invalid_dispatch_mouse_request'});
    return;
  }
  const target = {tabId};
  let attached = false;
  try {
    await chrome.tabs.get(tabId);
    await debuggerAttach(target);
    attached = true;
    await debuggerCommand(target, 'Emulation.setFocusEmulationEnabled', {enabled: true});
    await debuggerCommand(target, 'Input.dispatchMouseEvent', {
      type: 'mouseMoved', x, y, button: 'none', buttons: 0, pointerType: 'mouse'
    });
    for (let index = 1; index <= clickCount; index += 1) {
      await debuggerCommand(target, 'Input.dispatchMouseEvent', {
        type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: index, pointerType: 'mouse'
      });
      await debuggerCommand(target, 'Input.dispatchMouseEvent', {
        type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: index, pointerType: 'mouse'
      });
    }
    send({type: 'result', request_id: requestId, ok: true, chrome_tab_id: tabId, dispatched: true, click_count: clickCount});
  } catch (error) {
    send({
      type: 'result', request_id: requestId, ok: false,
      error: 'chrome_dispatch_mouse_failed', message: String(error && error.message || error || 'unknown')
    });
  } finally {
    if (attached) {
      try { await debuggerCommand(target, 'Emulation.setFocusEmulationEnabled', {enabled: false}); } catch (_) {}
      try { await debuggerDetach(target); } catch (_) {}
    }
  }
}


async function handleSetFileInput(message) {
  const requestId = String(message.request_id || '');
  const tabId = Number(message.chrome_tab_id);
  const selector = String(message.css_selector || '');
  const filePath = String(message.file_path || '');
  if (!requestId || !Number.isInteger(tabId) || tabId < 0 || !selector || selector.length > 10000 || !filePath || filePath.length > 4096 || filePath.includes('\0')) {
    send({type: 'result', request_id: requestId, ok: false, error: 'invalid_set_file_input_request'});
    return;
  }
  const target = {tabId};
  let attached = false;
  try {
    await chrome.tabs.get(tabId);
    await debuggerAttach(target);
    attached = true;
    const expression = `document.querySelector(${JSON.stringify(selector)})`;
    const lookup = await debuggerCommand(target, 'Runtime.evaluate', {
      expression, returnByValue: false, awaitPromise: false, userGesture: false
    });
    if (lookup.exceptionDetails) throw new Error(lookup.exceptionDetails.text || 'file_input_lookup_failed');
    const remote = lookup.result || {};
    if (!remote.objectId) throw new Error('file_input_not_found');
    await debuggerCommand(target, 'DOM.setFileInputFiles', {
      files: [filePath], objectId: remote.objectId
    });
    const verifyExpr = `(()=>{const el=document.querySelector(${JSON.stringify(selector)});const f=el&&el.files&&el.files[0];return f?JSON.stringify({count:el.files.length,name:f.name,size:f.size,lastModified:f.lastModified}):''})()`;
    const verify = await debuggerCommand(target, 'Runtime.evaluate', {
      expression: verifyExpr, returnByValue: true, awaitPromise: false, userGesture: false
    });
    if (verify.exceptionDetails) throw new Error(verify.exceptionDetails.text || 'file_input_verify_failed');
    const metadata = String((verify.result || {}).value || '');
    if (!metadata) throw new Error('file_input_not_set');
    send({type: 'result', request_id: requestId, ok: true, chrome_tab_id: tabId, metadata});
  } catch (error) {
    send({
      type: 'result', request_id: requestId, ok: false,
      error: 'chrome_set_file_input_failed', message: String(error && error.message || error || 'unknown')
    });
  } finally {
    if (attached) { try { await debuggerDetach(target); } catch (_) {} }
  }
}

function connect() {
  if (!PORT || !TOKEN) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  clearTimers();
  const ws = new WebSocket(`ws://127.0.0.1:${PORT}/chrome-background-bridge`);
  socket = ws;
  ws.addEventListener('open', () => {
    send({type: 'hello', token: TOKEN, features: FEATURES, version: chrome.runtime.getManifest().version});
    pingTimer = setInterval(() => send({type: 'ping'}), 20000);
  });
  ws.addEventListener('message', (event) => {
    let message = null;
    try { message = JSON.parse(String(event.data || '')); } catch (_) { return; }
    if (message && message.type === 'open_tab') { void handleOpenTab(message); return; }
    const handlers = {
      execute_js: handleExecuteJs, dispatch_mouse: handleDispatchMouse, set_file_input: handleSetFileInput,
      handle_dialog: handleDialog, gesture: handleGesture
    };
    const handler = message && handlers[message.type];
    if (handler) void withTab(Number(message.chrome_tab_id), () => handler(message));
  });
  ws.addEventListener('close', () => {
    if (socket === ws) socket = null;
    clearTimers();
    scheduleReconnect();
  });
  ws.addEventListener('error', () => {
    try { ws.close(); } catch (_) {}
  });
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
// A restarted server closes the socket while Chrome may have no page loading to wake this
// worker; a periodic alarm wakes it so it reconnects on its own.
if (chrome.alarms) {
  chrome.alarms.create('mac-mcp-reconnect', {periodInMinutes: 0.5});
  chrome.alarms.onAlarm.addListener((alarm) => { if (alarm && alarm.name === 'mac-mcp-reconnect') connect(); });
}
chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === 'mac_mcp_bridge_wake') connect();
});
connect();
