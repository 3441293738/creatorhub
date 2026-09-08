// Feedback follows real state; no write, focus, or selected value waits for motion.
const media = window.matchMedia("(prefers-reduced-motion: reduce)");
const running = new Set();
const byElement = new WeakMap();
const closing = new Map();
const legacy = new WeakMap();
export const reducedMotion = () => media.matches || document.documentElement.dataset.motion === "reduced";
const ease = "cubic-bezier(.22,1,.36,1)";

export function animate(element, frames, duration = 180) {
  if (!element) return null;
  byElement.get(element)?.cancel();
  if (reducedMotion() || !element.animate || !element.isConnected) return null;
  const animation = element.animate(frames, { duration, easing: ease });
  byElement.set(element, animation); running.add(animation);
  const clean = () => { running.delete(animation); if (byElement.get(element) === animation) byElement.delete(element); };
  animation.finished.then(clean, clean);
  return animation;
}

export function reveal(element, axis = "y", direction = 1) {
  return animate(element, [
    { opacity: .45, transform: `translate${axis.toUpperCase()}(${direction * 6}px)` },
    { opacity: 1, transform: "none" },
  ], 180);
}

// Cancellation is explicit and has a timeout fallback. Required cleanup never
// depends solely on animationend (including mid-animation preference changes).
export function cancelExit(element) {
  if (!element) return;
  const state = closing.get(element);
  if (!state) return;
  clearTimeout(state.timer); closing.delete(element); state.animation?.cancel();
  element.inert = state.inert;
  element.removeAttribute("data-ui-closing");
}
export function exit(element, finish, { transform = "translateY(4px)", duration = 120 } = {}) {
  cancelExit(element);
  if (!element || reducedMotion() || !element.animate) { finish(); return; }
  const state = { inert: element.inert, animation: null, timer: null, finish: null };
  element.dataset.uiClosing = "true"; element.inert = true;
  state.finish = () => {
    if (closing.get(element) !== state) return;
    cancelExit(element); finish();
  };
  closing.set(element, state);
  state.animation = animate(element, [{ opacity: 1, transform: "none" }, { opacity: 0, transform }], duration);
  state.timer = setTimeout(state.finish, duration + 50);
  state.animation?.finished.then(state.finish, () => {});
}

function applyMotionPreference() {
  if (!reducedMotion()) return;
  [...closing.values()].forEach(state => state.finish());
  [...running].forEach(animation => animation.cancel());
}
media.addEventListener("change", applyMotionPreference);
window.addEventListener("creatorhub:appearance", applyMotionPreference);

// The selected control always changes immediately. Only its decorative marker
// travels. FLIP animates transform, never layout width/left, and rebases on resize.
export function selectionMarker(container, selector, variant = "segment") {
  if (!container) return () => {};
  const marker = document.createElement("span");
  marker.className = `wb-selection-marker wb-marker-${variant}`;
  marker.setAttribute("aria-hidden", "true");
  container.prepend(marker); container.dataset.marker = variant;
  let previous = null, frame = null;
  const update = (motion = true) => {
    const selected = container.querySelector(selector);
    if (!selected || !container.getClientRects().length) { marker.hidden = true; previous = null; return; }
    const old = previous && marker.getBoundingClientRect();
    byElement.get(marker)?.cancel();
    const target = selected.getBoundingClientRect(), parent = container.getBoundingClientRect();
    const width = target.width, height = variant === "line" ? 2 : target.height;
    if (!width) { marker.hidden = true; previous = null; return; }
    const left = target.left - parent.left + container.scrollLeft - container.clientLeft;
    const top = variant === "line" ? container.scrollHeight - 2 : target.top - parent.top + container.scrollTop - container.clientTop;
    marker.hidden = false;
    marker.style.cssText = `left:${left}px;top:${top}px;width:${width}px;height:${height}px`;
    if (old && motion && (previous !== selected || Math.abs(old.left - target.left) > 1)) {
      const next = marker.getBoundingClientRect();
      animate(marker, [{ transform: `translate(${old.left - next.left}px,${old.top - next.top}px) scaleX(${old.width / width})` }, { transform: "none" }], 220);
    } else if (!motion) byElement.get(marker)?.cancel();
    previous = selected;
  };
  const observer = new MutationObserver(() => {
    cancelAnimationFrame(frame); frame = requestAnimationFrame(() => update());
  });
  observer.observe(container, { subtree: true, attributes: true, attributeFilter: ["class", "aria-pressed", "aria-selected", "data-state"] });
  const resize = new ResizeObserver(() => update(false)); resize.observe(container);
  const layout = () => update(false);
  window.addEventListener("creatorhub:appearance", layout);
  update(false);
  return () => { observer.disconnect(); resize.disconnect(); cancelAnimationFrame(frame); window.removeEventListener("creatorhub:appearance", layout); byElement.get(marker)?.cancel(); marker.remove(); delete container.dataset.marker; };
}

export function installMotion() {
  selectionMarker(document.querySelector(".theme-switch"), '[aria-pressed="true"]');
  selectionMarker(document.querySelector(".pswitch"), '[aria-selected="true"]');
  document.querySelectorAll(".tabbar").forEach(bar => selectionMarker(bar, ".tab.active"));
  document.addEventListener("toggle", event => {
    const details = event.target;
    if (details.tagName !== "DETAILS" || !details.open) return;
    reveal(details.querySelector(".collection-advanced-body,.wb-filter-body") || details.querySelector(":scope > :not(summary)"));
  }, true);
  document.addEventListener("click", event => {
    const theme = event.target.closest("[data-theme-choice]");
    if (theme) animate(theme.querySelector("svg"), [{ opacity: .5, transform: "rotate(-18deg) scale(.85)" }, { opacity: 1, transform: "none" }], 220);
  });
  // All existing dialogs use these hooks, retaining their business/focus logic.
  window.CreatorHubMotion = {
    reveal, exit, cancelExit,
    modalOpened(element) {
      cancelExit(element);
      legacy.set(element, { display: element.style.display || "flex" });
      animate(element, [{ opacity: 0 }, { opacity: 1 }], 200);
      const mobile = element.dataset.editor === "true" && matchMedia("(max-width:600px)").matches;
      animate(element.querySelector(".rp-box,.pv-box"), [{ transform: mobile ? "translateY(24px)" : "translateY(10px) scale(.985)" }, { transform: "none" }], 240);
    },
    modalClosed(element) {
      const state = legacy.get(element);
      if (!state || reducedMotion()) return;
      element.style.display = state.display;
      animate(element.querySelector(".rp-box,.pv-box"), [{ transform: "none" }, { transform: "translateY(6px) scale(.995)" }], 140);
      exit(element, () => { element.style.display = "none"; legacy.delete(element); }, { transform: "none" });
    },
  };
}
