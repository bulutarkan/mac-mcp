// Dark, Light or Device: follows the Mac until the visitor picks one, then remembers it.
// Loaded in <head> so a remembered choice is applied before the first paint.

const root = document.documentElement;
const KEY = "mac-mcp-theme";
const CHOICES = ["dark", "light", "system"];
root.classList.add("js");

try {
  const saved = localStorage.getItem(KEY) || localStorage.getItem("theme");
  if (CHOICES.includes(saved)) root.dataset.theme = saved;
} catch {}

document.addEventListener("DOMContentLoaded", () => {
  const group = document.querySelector(".seg");
  if (!group) return;
  const buttons = [...group.querySelectorAll("[data-theme-choice]")];
  const mark = () => {
    const now = root.dataset.theme || "system";
    for (const button of buttons) button.setAttribute("aria-pressed", String(button.dataset.themeChoice === now));
  };
  mark();
  group.hidden = false;

  for (const button of buttons) {
    button.addEventListener("click", () => {
      const next = button.dataset.themeChoice;
      try { localStorage.setItem(KEY, next); } catch {}
      const apply = () => { root.dataset.theme = next; mark(); };
      if (!document.startViewTransition || matchMedia("(prefers-reduced-motion: reduce)").matches) return apply();

      const box = button.getBoundingClientRect();
      const x = box.left + box.width / 2;
      const y = box.top + box.height / 2;
      const r = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
      document.startViewTransition(apply).ready.then(() => {
        root.animate(
          { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${r}px at ${x}px ${y}px)`] },
          { duration: 600, easing: "cubic-bezier(0.22, 0.8, 0.24, 1)", pseudoElement: "::view-transition-new(root)" },
        );
      });
    });
  }
});
