import assert from 'node:assert/strict';
import { McpAppBridge } from './src/bridge.js';
let onMessage;
const observed = [];
const parent = { postMessage(packet) {
  observed.push(packet);
  if (packet.id != null) queueMicrotask(() => onMessage({
    source: parent, data: { jsonrpc: '2.0', id: packet.id, result: packet.method === 'ui/initialize'
      ? { hostContext: { displayMode: 'fullscreen', safeAreaInsets: { top: 0, right: 0, bottom: 72, left: 0 } } }
      : { ok: true } },
  }));
} };
globalThis.window = {
  parent,
  addEventListener(type, callback) { if (type === 'message') onMessage = callback; },
};
const app = new McpAppBridge({ name: 'Mac MCP', version: '0.1.1' });
const contexts = [];
app.onhostcontext = (context) => contexts.push(context);
await app.connect();
assert.equal(contexts[0].safeAreaInsets.bottom, 72);
assert.equal(observed[0].method, 'ui/initialize');
assert.equal(observed[1].method, 'ui/notifications/initialized');
await app.callServerTool({ name: 'mac_mcp_panel_state', arguments: { description: 'Refresh Mac MCP' } });
assert.equal(observed[2].method, 'tools/call');
assert.equal(observed[2].params.name, 'mac_mcp_panel_state');
let delivered = false;
app.ontoolresult = (result) => { delivered = result.structuredContent?.ok === true; };
onMessage({ source: parent, data: { jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: {ok:true} } } });
assert.equal(delivered, true);
onMessage({ source: parent, data: { jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: { displayMode: 'inline' } } });
assert.equal(contexts[1].displayMode, 'inline');
console.log('PASS: bridge initialization, host notification, tool call, tool-result dispatch, host context');
