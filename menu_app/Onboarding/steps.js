// The onboarding's state, kept apart from the DOM so it can be tested with `node --test`.

export const SCREENS = [
  { id: 'welcome', kind: 'tour', rail: 'Tour' },
  { id: 'apps', kind: 'tour', rail: 'Tour' },
  { id: 'browser', kind: 'tour', rail: 'Tour' },
  { id: 'safety', kind: 'tour', rail: 'Tour' },
  { id: 'start', kind: 'setup', rail: 'Start' },
  { id: 'permissions', kind: 'setup', rail: 'Permissions' },
  { id: 'connect', kind: 'setup', rail: 'Connect' },
  { id: 'request', kind: 'setup', rail: 'First request' },
  { id: 'done', kind: 'finish', rail: 'Done' },
];

// Clients `mac-mcp connect-config` knows; the app renders their setup, never this page.
export const CLIENTS = [
  { id: 'claude-code', name: 'Claude Code', needsRemote: false },
  { id: 'codex', name: 'Codex', needsRemote: false },
  { id: 'opencode', name: 'OpenCode', needsRemote: false },
  { id: 'claude-desktop', name: 'Claude Desktop', needsRemote: false },
  { id: 'chatgpt', name: 'ChatGPT', needsRemote: true },
];

export const FIRST_REQUEST = 'Use Mac MCP to tell me which macOS version this Mac is running.';

export function indexOf(id) {
  return SCREENS.findIndex((screen) => screen.id === id);
}

/** What the live status (GET /dashboard/api/onboarding plus server_running) says is done. */
export function evaluate(status) {
  const s = status || {};
  const running = s.server_running === true;
  const accessibility = running ? s.accessibility || 'unknown' : 'unknown';
  const screen = running ? s.screen_recording || 'unknown' : 'unknown';
  return {
    start: running,
    connect: running && Boolean(s.first_call),
    request: running && Boolean(s.first_success),
    firstTool: (s.first_call && s.first_call.tool) || null,
    permissions: {
      accessibility,
      screen_recording: screen,
      done: accessibility === 'granted' && screen === 'granted',
    },
    holderIsMacMCP: s.permission_holder === 'Mac MCP',
    holder: s.permission_holder || null,
    remote: Boolean(s.public_endpoint_mode) && s.public_endpoint_mode !== 'none',
  };
}

/** The primary button for a screen: its label and what it does. */
export function primaryAction(id, evaluation) {
  const e = evaluation || evaluate(null);
  switch (id) {
    case 'welcome':
      return { label: 'Show me how it works', action: 'next' };
    case 'safety':
      return { label: 'Set up Mac MCP', action: 'next' };
    case 'start':
      return e.start ? { label: 'Continue', action: 'next' } : { label: 'Start Mac MCP', action: 'startServer' };
    case 'permissions':
      return { label: e.permissions.done ? 'Continue' : 'Allow later', action: 'next' };
    case 'connect':
      return { label: e.connect ? 'Continue' : 'Skip for now', action: 'next' };
    case 'request':
      return { label: e.request ? 'Continue' : 'Skip for now', action: 'next' };
    case 'done':
      return { label: 'Finish', action: 'finish' };
    default:
      return { label: 'Continue', action: 'next' };
  }
}
