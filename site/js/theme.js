// Light or dark: follows the Mac until the visitor picks one with the switch, then remembers it.
// Loaded in <head> so a remembered choice is applied before the first paint.

const root = document.documentElement;
const systemDark = matchMedia("(prefers-color-scheme: dark)");

try {
  const saved = localStorage.getItem("theme");
  if (saved === "light" || saved === "dark") root.dataset.theme = saved;
} catch {}

const current = () => root.dataset.theme || (systemDark.matches ? "dark" : "light");

document.addEventListener("DOMContentLoaded", () => {
  const button = document.querySelector("button.theme");
  if (!button) return;
  const label = () => button.setAttribute("aria-label", current() === "dark" ? "Use light theme" : "Use dark theme");
  label();
  systemDark.addEventListener("change", label);
  button.hidden = false;

  button.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    try { localStorage.setItem("theme", next); } catch {}
    const apply = () => { root.dataset.theme = next; label(); };
    if (!document.startViewTransition || matchMedia("(prefers-reduced-motion: reduce)").matches) return apply();

    const box = button.getBoundingClientRect();
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    const r = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    document.startViewTransition(apply).ready.then(() => {
      root.animate(
        { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${r}px at ${x}px ${y}px)`] },
        { duration: 560, easing: "cubic-bezier(0.22, 0.8, 0.24, 1)", pseudoElement: "::view-transition-new(root)" },
      );
    });
  });
});
