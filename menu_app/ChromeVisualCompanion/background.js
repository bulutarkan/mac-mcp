'use strict';

try { importScripts('bridge_config.js'); } catch (_) {}

const CONFIG = globalThis.MAC_MCP_CHROME_BRIDGE || {};
const PORT = Number(CONFIG.port || 0);
const TOKEN = String(CONFIG.token || '');
const RECONNECT_MS = Math.max(250, Math.min(Number(CONFIG.reconnect_ms || 1000), 10000));
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


function debuggerAttach(target) {
  return new Promise((resolve, reject) => {
    chrome.debugger.attach(target, '1.3', () => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message || 'debugger_attach_failed'));
      else resolve();
    });
  });
}

function debuggerDetach(target) {
  return new Promise((resolve) => {
    chrome.debugger.detach(target, () => resolve());
  });
}

function debuggerCommand(target, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand(target, method, params || {}, (result) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message || 'debugger_command_failed'));
      else resolve(result || {});
    });
  });
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
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab.active === true) { /* active is allowed; transport itself never activates it */ }
    await debuggerAttach(target);
    attached = true;
    const out = await debuggerCommand(target, 'Runtime.evaluate', {
      expression: js, returnByValue: true, awaitPromise: true, userGesture: false
    });
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
    send({type: 'hello', token: TOKEN});
    pingTimer = setInterval(() => send({type: 'ping'}), 20000);
  });
  ws.addEventListener('message', (event) => {
    let message = null;
    try { message = JSON.parse(String(event.data || '')); } catch (_) { return; }
    if (message && message.type === 'open_tab') void handleOpenTab(message);
    else if (message && message.type === 'execute_js') void handleExecuteJs(message);
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
connect();
