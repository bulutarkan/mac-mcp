// Header, scroll reveals, the pointer-following light, tabs, copy buttons, the live star count and the
// small interactive examples. The page works without this file.

export function copyText(button, readTarget, writeClipboard) {
  const text = readTarget(button.dataset.copy);
  if (!text) return Promise.resolve(false);
  const label = button.querySelector?.("span") ?? button;
  return writeClipboard(text).then(
    () => {
      label.textContent = "Copied";
      return true;
    },
    () => {
      label.textContent = "Select it";
      return false;
    },
  );
}

export function formatStars(count) {
  return count >= 1000 ? `${(count / 1000).toFixed(count >= 10000 ? 0 : 1).replace(/\.0$/, "")}k` : String(count);
}

if (typeof document !== "undefined") {
  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
  const root = document.documentElement;
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fine = matchMedia("(pointer: fine)").matches;

  // Copy buttons.
  for (const button of $$("button.copy")) {
    button.hidden = false;
    button.addEventListener("click", () => {
      copyText(button, (id) => document.getElementById(id)?.textContent.trim(), (text) => navigator.clipboard.writeText(text))
        .then((ok) => {
          button.classList.toggle("done", ok);
          setTimeout(() => { button.classList.remove("done"); button.querySelector("span").textContent = "Copy"; }, 1600);
        });
    });
  }

  // Sticky header: glass once the page moves, a progress line, and the hero light drifting with the scroll.
  const top = $(".top");
  const bar = $(".progress");
  let ticking = false;
  const onScroll = () => {
    ticking = false;
    const y = scrollY;
    top.classList.toggle("stuck", y > 8);
    const max = document.documentElement.scrollHeight - innerHeight;
    bar.style.setProperty("--p", String(max > 0 ? y / max : 0));
    if (!still) root.style.setProperty("--sy", String(Math.min(y, 1200)));
  };
  addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
  onScroll();

  // Small screens: the sections live in a sheet that slides open over the page, under the header.
  const menuButton = $(".menu-btn");
  const sheet = $("#sheet");
  menuButton.hidden = false;
  sheet.hidden = false;
  sheet.inert = true;
  $$("a", sheet).forEach((link, index) => link.style.setProperty("--i", String(index)));
  const setSheet = (open) => {
    sheet.classList.toggle("open", open);
    sheet.inert = !open;
    top.classList.toggle("menu-open", open);
    menuButton.setAttribute("aria-expanded", String(open));
    menuButton.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  };
  const sheetOpen = () => sheet.classList.contains("open");
  menuButton.addEventListener("click", () => setSheet(!sheetOpen()));
  sheet.addEventListener("click", (event) => { if (event.target.closest("a")) setSheet(false); });
  document.addEventListener("click", (event) => { if (sheetOpen() && !top.contains(event.target)) setSheet(false); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && sheetOpen()) { setSheet(false); menuButton.focus(); } });
  matchMedia("(min-width: 1081px)").addEventListener("change", (event) => { if (event.matches) setSheet(false); });

  // Questions: one answer open at a time, opening and closing with a slide.
  const answers = $$(".qa details");
  const slide = (item, open) => {
    const body = item.querySelector("p");
    item.animation?.cancel();
    item.classList.toggle("closing", !open);
    if (open) item.open = true;
    if (still) { item.open = open; item.classList.remove("closing"); return; }
    const full = `${body.scrollHeight}px`;
    const shut = { height: "0px", paddingBottom: "0px", opacity: 0 };
    const shown = { height: full, paddingBottom: getComputedStyle(body).paddingBottom, opacity: 1 };
    body.style.overflow = "hidden";
    item.animation = body.animate(open ? [shut, shown] : [{ ...shown, height: `${body.offsetHeight}px` }, shut],
      { duration: open ? 380 : 280, easing: "cubic-bezier(0.22, 0.8, 0.24, 1)" });
    item.animation.onfinish = () => {
      body.style.overflow = "";
      item.animation = null;
      if (!open) { item.open = false; item.classList.remove("closing"); }
    };
  };
  for (const item of answers) {
    item.querySelector("summary").addEventListener("click", (event) => {
      event.preventDefault();
      const opening = !item.open || item.classList.contains("closing");
      if (opening) for (const other of answers) if (other !== item && other.open && !other.classList.contains("closing")) slide(other, false);
      slide(item, opening);
    });
  }

  // Each section reveals its own parts, in order, the first time it comes into view.
  const reveal = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("in");
      reveal.unobserve(entry.target);
      setTimeout(() => $$(".r", entry.target).forEach((part) => part.classList.add("settled")), 1600);
    }
  }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
  for (const section of $$("[data-reveal]")) {
    $$(".r", section).forEach((part, index) => part.style.setProperty("--i", String(Math.min(index, 8))));
    reveal.observe(section);
  }

  // The nav marks the section being read, with a pill that slides to it.
  const nav = $(".nav");
  const pill = document.createElement("span");
  pill.className = "nav-pill";
  nav.prepend(pill);
  const links = $$("a", nav);
  const movePill = (link) => {
    links.forEach((item) => item.setAttribute("aria-current", String(item === link)));
    if (!link) { pill.style.opacity = "0"; return; }
    pill.style.opacity = "1";
    pill.style.width = `${link.offsetWidth}px`;
    pill.style.transform = `translateX(${link.offsetLeft}px)`;
  };
  const spy = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) movePill(links.find((link) => link.hash === `#${entry.target.id}`) ?? null);
    }
  }, { rootMargin: "-40% 0px -55% 0px" });
  for (const section of $$("main > section[id]")) spy.observe(section);
  addEventListener("resize", () => movePill(links.find((link) => link.getAttribute("aria-current") === "true") ?? null), { passive: true });

  // The light follows the pointer: the page grid, each card's rim, and a slight tilt of the demo screen.
  if (fine && !still) {
    let frame = 0;
    let last = null;
    addEventListener("pointermove", (event) => {
      last = event;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        // The page may be zoomed (90 % on desktop): pointer and box positions come in screen pixels.
        const zoom = parseFloat(getComputedStyle(root).zoom) || 1;
        root.style.setProperty("--mx", `${last.clientX / zoom}px`);
        root.style.setProperty("--my", `${last.clientY / zoom}px`);
        const card = last.target.closest?.(".glow");
        if (card) {
          const box = card.getBoundingClientRect();
          card.style.setProperty("--x", `${(last.clientX - box.left) / zoom}px`);
          card.style.setProperty("--y", `${(last.clientY - box.top) / zoom}px`);
        }
      });
    }, { passive: true });

    const tilt = $(".tilt");
    tilt?.addEventListener("pointermove", (event) => {
      const box = tilt.getBoundingClientRect();
      tilt.style.setProperty("--ry", `${((event.clientX - box.left) / box.width - 0.5) * 5}deg`);
      tilt.style.setProperty("--rx", `${((event.clientY - box.top) / box.height - 0.5) * -4}deg`);
    });
    tilt?.addEventListener("pointerleave", () => { tilt.style.setProperty("--rx", "0deg"); tilt.style.setProperty("--ry", "0deg"); });

    for (const button of $$(".magnet")) {
      button.addEventListener("pointermove", (event) => {
        const box = button.getBoundingClientRect();
        button.style.transform = `translate(${(event.clientX - box.left - box.width / 2) * 0.12}px, ${(event.clientY - box.top - box.height / 2) * 0.2}px)`;
      });
      button.addEventListener("pointerleave", () => { button.style.transform = ""; });
    }
  }

  // Tabs: the install commands and the permission profiles.
  for (const list of $$('[role="tablist"]')) {
    const tabs = $$('[role="tab"]', list);
    const select = (tab, focus) => {
      for (const item of tabs) {
        const on = item === tab;
        item.setAttribute("aria-selected", String(on));
        item.tabIndex = on ? 0 : -1;
        document.getElementById(item.getAttribute("aria-controls")).hidden = !on;
      }
      if (focus) tab.focus();
    };
    for (const tab of tabs) {
      tab.addEventListener("click", () => select(tab));
      tab.addEventListener("keydown", (event) => {
        const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        if (step) select(tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length], true);
      });
    }
  }

  // Connect: the command and what it does, for the chosen app.
  const NOTES = {
    "claude-desktop": "Adds Mac MCP to Claude Desktop's config and keeps a backup of the old file. Restart Claude Desktop.",
    "claude-code": "Registers Mac MCP with Claude Code's own mcp add command.",
    codex: "Registers Mac MCP with Codex's own mcp add command.",
    opencode: "Adds Mac MCP to OpenCode's config and keeps a backup of the old file.",
    cline: "Adds Mac MCP to Cline's config and keeps a backup of the old file.",
    goose: "Adds Mac MCP to Goose's config and keeps a backup of the old file.",
    chatgpt: "Copies the connector address to your clipboard. ChatGPT needs a public HTTPS address: Cloudflare Tunnel, ngrok or your own.",
  };
  const chips = $$("[data-client]");
  for (const chip of chips) {
    chip.addEventListener("click", () => {
      chips.forEach((item) => item.setAttribute("aria-pressed", String(item === chip)));
      $("#connect-cmd").textContent = `mac-mcp connect --client ${chip.dataset.client}`;
      $("[data-client-note]").textContent = NOTES[chip.dataset.client];
    });
  }

  // The approval example answers like Server Approval does.
  const ask = $("[data-ask]");
  for (const button of $$("[data-answer]", ask)) {
    button.addEventListener("click", () => {
      const allowed = button.dataset.answer === "allow";
      ask.classList.toggle("allowed", allowed);
      ask.classList.toggle("blocked", !allowed);
      $(".ask-out", ask).textContent = allowed
        ? "Allowed once. The email goes out; the next one asks again."
        : "Blocked. Nothing was sent, and your AI app is told so.";
    });
  }

  // The live star count from GitHub, kept for an hour.
  const showStars = (count) => {
    for (const slot of $$("[data-stars]")) { slot.textContent = formatStars(count); slot.hidden = false; }
    for (const link of $$(".star")) link.setAttribute("aria-label", `Mac MCP on GitHub, ${count} stars`);
  };
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem("mac-mcp-stars") || "null"); } catch {}
  if (cached && Date.now() - cached.at < 3_600_000) showStars(cached.count);
  else {
    fetch("https://api.github.com/repos/bulutarkan/mac-mcp", { headers: { Accept: "application/vnd.github+json" } })
      .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
      .then((repo) => {
        if (!Number.isFinite(repo.stargazers_count)) return;
        showStars(repo.stargazers_count);
        try { localStorage.setItem("mac-mcp-stars", JSON.stringify({ count: repo.stargazers_count, at: Date.now() })); } catch {}
      })
      .catch(() => { if (cached) showStars(cached.count); });
  }
}
