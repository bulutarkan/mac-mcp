/** Small JSON-RPC bridge implementing the MCP Apps postMessage protocol. */
export class McpAppBridge {
  constructor({ name, version }) {
    this.name = name;
    this.version = version;
    this.nextId = 1;
    this.pending = new Map();
    this.ontoolresult = null;
    this.onhostcontext = null;
    window.addEventListener('message', (event) => {
      if (event.source !== window.parent) return;
      const data = event.data;
      if (!data || data.jsonrpc !== '2.0') return;
      if (data.id !== undefined && this.pending.has(data.id)) {
        const { resolve, reject } = this.pending.get(data.id);
        this.pending.delete(data.id);
        if (data.error) reject(new Error(data.error.message || 'Host request failed'));
        else resolve(data.result);
      } else if (data.method === 'ui/notifications/tool-result') {
        this.ontoolresult?.(data.params);
      } else if (data.method === 'ui/notifications/host-context-changed') {
        this.onhostcontext?.(data.params || {});
      }
    });
  }
  request(method, params) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      window.parent.postMessage({ jsonrpc: '2.0', id, method, params }, '*');
    });
  }
  notify(method, params = {}) {
    window.parent.postMessage({ jsonrpc: '2.0', method, params }, '*');
  }
  async connect() {
    const result = await this.request('ui/initialize', {
      appInfo: { name: this.name, version: this.version },
      appCapabilities: {},
      protocolVersion: '2026-01-26',
    });
    this.notify('ui/notifications/initialized');
    if (result?.hostContext) this.onhostcontext?.(result.hostContext);
    return result;
  }
  callServerTool({ name, arguments: args }) {
    return this.request('tools/call', { name, arguments: args ?? {} });
  }
}
