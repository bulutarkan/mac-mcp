'use strict';

// MV3 service workers may be dormant even after browser startup. A content-script
// message is an extension event, so Chrome wakes the worker without activating
// the tab or foregrounding the browser. The worker's top-level connect() then
// establishes the localhost bridge.
try {
  chrome.runtime.sendMessage({type: 'mac_mcp_bridge_wake'}, () => void chrome.runtime.lastError);
} catch (_) {}
