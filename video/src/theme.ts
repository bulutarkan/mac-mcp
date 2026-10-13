// The Mac MCP palette (menu_app/Onboarding/onboarding.css, dark variant) and the live island's colors.
export const theme = {
  night: "#151b2e",
  glass: "#222a44",
  glassEdge: "rgba(236, 239, 246, 0.09)",
  paper: "#eceff6",
  haze: "#8e97b2",
  ember: "#ffb45e",
  emberInk: "#2a1a05",
  teal: "#5ed3c4",
  violet: "#8b7cf6",
  ok: "#5ed3a0",
  working: "#7390ff",
  attention: "#ffa840",
  serif: 'ui-serif, "New York", Georgia, serif',
  sans: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif',
  mono: 'ui-monospace, "SF Mono", Menlo, monospace',
};

export const FPS = 30;
export const seconds = (s: number) => Math.round(s * FPS);
