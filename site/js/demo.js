// The demo scenes and the island. A scene's time plays the video from there; the scene on screen is
// marked and the rail fills; the island names what is happening, as the app's live island does: a
// turning ring while the demo plays, a tick when it ends, otherwise the section being read.
// The page works without this file.

const island = document.querySelector(".island");
const face = island?.querySelector(".island-face");
const label = island?.querySelector(".island-label");
const video = document.querySelector("#demo video");
const rail = document.querySelector(".chapters");
const scenes = [...document.querySelectorAll(".chapter")];

function say(text, state) {
  if (!label) return;
  label.textContent = text;
  island.dataset.state = state;
}

function setOpen(open) {
  if (!face) return;
  face.setAttribute("aria-expanded", String(open));
  island.classList.toggle("open", open);
}

face?.addEventListener("click", () => setOpen(face.getAttribute("aria-expanded") !== "true"));
island?.addEventListener("click", (event) => { if (event.target.closest("a")) setOpen(false); });
document.addEventListener("click", (event) => { if (island && !island.contains(event.target)) setOpen(false); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && island?.classList.contains("open")) { setOpen(false); face.focus(); }
});

if (video && rail) {
  const scene = () => scenes.findLast((item) => video.currentTime >= Number(item.dataset.at)) ?? scenes[0];
  const show = () => {
    const now = scene();
    for (const item of scenes) {
      if (item === now) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    }
    if (video.duration) rail.style.setProperty("--progress", String(video.currentTime / video.duration));
    if (!video.paused) say(now.dataset.island, "busy");
  };
  video.addEventListener("timeupdate", show);
  video.addEventListener("play", show);
  video.addEventListener("pause", () => { if (!video.ended) say(scene().dataset.island, "idle"); });
  video.addEventListener("ended", () => say("Done", "done"));

  for (const item of scenes) {
    const button = item.querySelector("button.at");
    button.disabled = false;
    button.addEventListener("click", () => {
      const go = () => {
        video.currentTime = Number(item.dataset.at);
        video.play().catch(() => {});
      };
      if (video.readyState >= 1) return go();
      // preload="none": nothing is loaded yet, so a seek would be lost.
      video.addEventListener("loadedmetadata", go, { once: true });
      video.preload = "metadata";
      video.load();
    });
  }
}

// While the demo is not playing, the island names the part of the page being read.
if (island && "IntersectionObserver" in window) {
  const watch = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting && (!video || video.paused)) say(entry.target.dataset.island, "idle");
    }
  }, { rootMargin: "-45% 0px -50% 0px" });
  for (const part of document.querySelectorAll("[data-island]")) watch.observe(part);
}
