// The demo film and the Safari example: small scenes drawn in HTML and driven by one clock, so they
// can be paused, scrubbed and replayed like a video. The agent cursor and its label copy the Visual
// Companion's. Every frame is computed from the time alone, so seeking always lands on the same picture.
// The page works without this file: the film shows the recording's poster instead.

const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
const ease = (k) => 1 - Math.pow(1 - k, 3);

// [class on the stage, from, to] in seconds.
const FILM = {
  duration: 40,
  classes: [
    ["cant", 1.9, 4.3], ["menu", 4.6, 8.7], ["press", 5.3, 5.45],
    ["c1", 5.5, 40], ["c2", 5.8, 40], ["c3", 6.1, 40], ["c4", 6.4, 40], ["c5", 6.7, 40],
    ["isl", 9.2, 36], ["finder", 9.1, 16.3], ["peek", 9.5, 15.6], ["p1", 9.5, 15.9], ["t1", 9.7, 40], ["ok1", 15.2, 40],
    ["fs1", 10.2, 10.75], ["fs2", 10.75, 11.3], ["fs3", 11.3, 11.85], ["fs4", 11.85, 12.4], ["fs5", 12.4, 12.95], ["fs6", 12.95, 13.5],
    ["found", 13.6, 16.3], ["done", 15.1, 16.2],
    ["safari", 16, 23.3], ["peek", 16.4, 22.5], ["p2", 16.4, 22.8], ["t2", 16.5, 40], ["ok2", 22.3, 40], ["frame", 16.5, 22.9],
    ["focus", 16.3, 22.9], ["on1", 16.6, 18.6], ["on2", 18.8, 19.9], ["on3", 20.1, 20.7], ["picked", 20.45, 23.3],
    ["scroll", 20.8, 23.3], ["saved", 21.85, 23], ["done", 22.3, 23.2],
    ["report", 23.3, 30.2], ["peek", 23.5, 29.3], ["p3", 23.5, 29.6], ["t3", 23.6, 40], ["ok3", 28.9, 40],
    ["k1", 24.3, 40], ["k2", 25.2, 40], ["k3", 26.1, 40], ["k4", 27, 40], ["files", 27.7, 40], ["sched", 28.3, 40], ["done", 28.9, 30.2],
    ["mail", 30.2, 36.2], ["peek", 30.5, 33.9], ["p4", 30.5, 34.2], ["attn", 30.5, 33.5], ["ask", 30.8, 33.35],
    ["t4", 30.6, 40], ["wait4", 30.7, 33.5], ["ok4", 33.6, 40], ["allowing", 32.9, 33.3], ["sent", 33.7, 36.2],
    ["done", 33.7, 36], ["fin", 34.4, 40], ["end", 36.2, 40],
  ],
  typing: [[".s-me", 0.3, 1.6], [".s-compose span", 17.2, 21.8], [".ff1 b", 17, 18.4], [".ff2 b", 19, 19.8]],
  // [time, x, y, label, click]; a point without x ends the track.
  tracks: [
    [".s-cursor", [
      [16.6, 690, 291, "Clicking", 1], [17, 690, 291, "Typing"], [18.5, 690, 291, "Typing"],
      [18.8, 670, 357, "Clicking", 1], [19, 670, 357, "Typing"], [19.9, 670, 357, "Typing"],
      [20.15, 700, 423, "Selecting", 1], [20.75, 700, 423, "Scrolling down"], [21.5, 604, 497, "Clicking", 1], [22.7],
    ]],
    [".s-pointer", [
      [3.9, 700, 420], [4.5, 1094, 15, null, 1], [5.3, 1026, 314, null, 1], [8.4, 1026, 314], [8.6],
      [31.3, 950, 640], [32.9, 715, 399, null, 1], [33.6],
    ]],
  ],
  still: 21.6,
};

const SAFARI = {
  duration: 10,
  classes: [["frame", 0.4, 8.8], ["on1", 0.6, 2.6], ["on2", 2.8, 6.2], ["scroll", 6.5, 10], ["saved", 7.45, 9.6]],
  typing: [[".mf1 b", 1, 2.3], [".mf2 b", 3.1, 5.9]],
  tracks: [
    [".s-cursor", [
      [0.6, 200, 121, "Clicking", 1], [1, 200, 121, "Typing"], [2.4, 200, 121, "Typing"],
      [2.8, 230, 198, "Clicking", 1], [3.1, 230, 198, "Typing"], [6, 230, 198, "Typing"],
      [6.4, 300, 240, "Scrolling down"], [7.2, 76, 301, "Clicking", 1], [8.6],
    ]],
  ],
  still: 7.6,
};

function scene(stage, spec) {
  const names = [...new Set(spec.classes.map(([name]) => name))];
  const typing = spec.typing.map(([selector, from, to]) => {
    const el = stage.querySelector(selector);
    return { el, text: el.dataset.type, from, to };
  });
  const tracks = spec.tracks.map(([selector, points]) => {
    const el = stage.querySelector(selector);
    return { el, tip: el.querySelector(".s-tip"), points, offset: selector === ".s-pointer" ? [6, 4] : [4.5, 4.5] };
  });
  const ripple = stage.querySelector(".s-ripple");
  const rail = stage.querySelector("[data-rail]");

  function place(track, t, before) {
    const { el, tip, points } = track;
    const i = points.findLastIndex((point) => point[0] <= t);
    if (i < 0 || points[i].length === 1) { el.classList.remove("on", "press"); return; }
    const a = points[i];
    const b = points[i + 1];
    let [x, y, label] = [a[1], a[2], a[3]];
    if (b && b.length > 1 && (b[1] !== a[1] || b[2] !== a[2])) {
      const move = Math.min(0.5, b[0] - a[0]);
      if (t >= b[0] - move) {
        const k = ease((t - b[0] + move) / move);
        x = a[1] + (b[1] - a[1]) * k;
        y = a[2] + (b[2] - a[2]) * k;
        label = b[3];
      }
    }
    el.classList.add("on");
    el.style.transform = `translate(${x - track.offset[0]}px, ${y - track.offset[1]}px)`;
    if (tip) tip.textContent = label || "";
    el.classList.toggle("press", points.some((point) => point[4] && t >= point[0] && t - point[0] < 0.15));
    const click = points.find((point) => point[4] && before < point[0] && t >= point[0] && t - point[0] < 0.4);
    if (click && ripple) {
      ripple.classList.remove("go");
      ripple.style.left = `${click[1]}px`;
      ripple.style.top = `${click[2]}px`;
      void ripple.offsetWidth;
      ripple.classList.add("go");
    }
  }

  return (t, before = t) => {
    const on = new Set(spec.classes.filter(([, from, to]) => t >= from && t < to).map(([name]) => name));
    for (const name of names) stage.classList.toggle(name, on.has(name));
    for (const item of typing) {
      const k = Math.max(0, Math.min(1, (t - item.from) / (item.to - item.from)));
      item.el.textContent = item.text.slice(0, Math.round(item.text.length * k));
      item.el.classList.toggle("typing", t >= item.from && t < item.to + 0.4);
    }
    for (const track of tracks) place(track, t, before);
    if (rail) rail.textContent = String(tracks[0].points.filter((point) => point.length > 1 && point[0] <= t).length);
  };
}

// A clock with play, pause and seek; it runs only while its scene is on screen and the tab is visible.
function player(spec, render, onTick) {
  let t = 0;
  let playing = false;
  let last = 0;
  let raf = 0;
  const tick = (now) => {
    const before = t;
    t += Math.min(0.1, (now - last) / 1000);
    last = now;
    if (t >= spec.duration) t = 0;
    render(t, t < before ? t : before);
    onTick?.(t, playing);
    raf = requestAnimationFrame(tick);
  };
  return {
    get time() { return t; },
    get playing() { return playing; },
    play() { if (playing) return; playing = true; last = performance.now(); raf = requestAnimationFrame(tick); onTick?.(t, true); },
    pause() { playing = false; cancelAnimationFrame(raf); onTick?.(t, false); },
    seek(to) { t = Math.max(0, Math.min(spec.duration - 0.01, to)); render(t); onTick?.(t, playing); },
  };
}

function fit(box, stage, width) {
  const set = () => stage.style.setProperty("--s", String(box.clientWidth / width));
  set();
  new ResizeObserver(set).observe(box);
}

function whenVisible(target, threshold, change) {
  let seen = false;
  new IntersectionObserver(([entry]) => { seen = entry.isIntersecting; change(seen && !document.hidden); }, { threshold }).observe(target);
  document.addEventListener("visibilitychange", () => change(seen && !document.hidden));
}

const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

// The film.
const film = document.querySelector("[data-film]");
if (film) {
  const screen = film.querySelector(".film-screen");
  const stage = film.querySelector("[data-stage]");
  const scrub = film.querySelector("[data-film-scrub]");
  const time = film.querySelector("[data-film-time]");
  const toggles = [...film.querySelectorAll("[data-film-toggle]")];
  const chapters = [...document.querySelectorAll(".chapter")];
  const starts = chapters.map((item) => Number(item.dataset.at));
  let userPaused = still;

  fit(screen, stage, 1200);
  const render = scene(stage, FILM);
  const show = (t, playing) => {
    film.classList.toggle("playing", playing);
    if (playing) film.classList.add("started");
    for (const button of toggles) button.setAttribute("aria-label", playing ? "Pause the demo" : "Play the demo");
    scrub.style.setProperty("--t", String(t / FILM.duration));
    scrub.setAttribute("aria-valuenow", String(Math.round(t)));
    scrub.setAttribute("aria-valuetext", `${Math.round(t)} seconds`);
    time.textContent = `${clock(t)} / ${clock(FILM.duration)}`;
    const now = starts.findLastIndex((start) => t >= start);
    chapters.forEach((item, index) => {
      item.classList.toggle("on", index === now);
      const end = starts[index + 1] ?? 36.2;
      item.style.setProperty("--cp", String(index === now ? Math.min(1, (t - starts[index]) / (end - starts[index])) : 0));
    });
  };
  const clip = player(FILM, render, show);

  film.querySelector(".film-bar").hidden = false;
  film.querySelector(".film-big").hidden = false;
  clip.seek(still ? FILM.still : 0);

  for (const button of toggles) {
    button.addEventListener("click", () => {
      if (clip.playing) { userPaused = true; clip.pause(); } else { userPaused = false; clip.play(); }
    });
  }
  whenVisible(screen, 0.45, (visible) => {
    if (visible && !userPaused) clip.play();
    else if (!visible) clip.pause();
  });

  const seekAt = (event) => {
    film.classList.add("started");
    const box = scrub.getBoundingClientRect();
    clip.seek(((event.clientX - box.left) / box.width) * FILM.duration);
  };
  scrub.addEventListener("pointerdown", (event) => {
    scrub.setPointerCapture(event.pointerId);
    seekAt(event);
    const move = (next) => seekAt(next);
    scrub.addEventListener("pointermove", move);
    scrub.addEventListener("pointerup", () => scrub.removeEventListener("pointermove", move), { once: true });
  });
  scrub.addEventListener("keydown", (event) => {
    const to = { ArrowRight: clip.time + 2, ArrowLeft: clip.time - 2, Home: 0, End: FILM.duration }[event.key];
    if (to === undefined) return;
    event.preventDefault();
    clip.seek(to);
  });

  chapters.forEach((item, index) => {
    const button = item.querySelector("button.at");
    button.disabled = false;
    button.addEventListener("click", () => {
      // With reduced motion a scene shows its finished moment instead of playing.
      clip.seek(still ? Math.min(starts[index] + 5, 39) : starts[index] + 0.01);
      if (!still) { userPaused = false; clip.play(); }
    });
  });

  // The real screen recording, in a dialog.
  const dialog = document.getElementById("rec");
  const video = dialog.querySelector("video");
  film.querySelector("[data-video-open]").addEventListener("click", () => {
    userPaused = true;
    clip.pause();
    dialog.showModal();
    video.play().catch(() => {});
  });
  dialog.querySelector("[data-video-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => video.pause());
}

// The Safari example in the features: loops while it is on screen.
const mini = document.querySelector("[data-mini]");
if (mini) {
  const stage = mini.querySelector("[data-stage]");
  fit(mini, stage, 560);
  const render = scene(stage, SAFARI);
  const clip = player(SAFARI, render);
  clip.seek(still ? SAFARI.still : 0);
  if (!still) whenVisible(mini, 0.4, (visible) => (visible ? clip.play() : clip.pause()));
}
