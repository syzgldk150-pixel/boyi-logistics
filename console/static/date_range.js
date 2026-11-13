// Shared behaviour for every Console date-range calendar: the quick presets
// (今天 / 昨天 / 当月) and mouse-wheel month paging. Each page keeps its own
// markup and selection state; only these two rules live here so they stay identical.
(function () {
  "use strict";

  function dayOnly(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
  }

  // Returns [start, end] as local Date objects, or null for an unknown preset.
  function quickRange(kind, now) {
    const today = dayOnly(now instanceof Date ? now : new Date());
    if (kind === "today") return [today, today];
    if (kind === "yesterday") {
      const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
      return [yesterday, yesterday];
    }
    if (kind === "month") {
      return [
        new Date(today.getFullYear(), today.getMonth(), 1),
        new Date(today.getFullYear(), today.getMonth() + 1, 0),
      ];
    }
    return null;
  }

  // Wheel down = next month, wheel up = previous month, while the pointer is over
  // `element`. Small trackpad deltas accumulate and a short cooldown keeps one
  // notch from skipping several months.
  function bindMonthWheel(element, onStep) {
    if (!(element instanceof Element) || typeof onStep !== "function") return () => {};
    const threshold = 40;
    const cooldownMs = 140;
    let accumulated = 0;
    let lastStepAt = 0;
    const handler = (event) => {
      if (event.ctrlKey) return; // keep pinch/ctrl+wheel zoom
      const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
      if (!delta) return;
      event.preventDefault();
      accumulated += delta;
      const now = Date.now();
      if (Math.abs(accumulated) < threshold || now - lastStepAt < cooldownMs) return;
      const step = accumulated > 0 ? 1 : -1;
      accumulated = 0;
      lastStepAt = now;
      onStep(step);
    };
    element.addEventListener("wheel", handler, { passive: false });
    return () => element.removeEventListener("wheel", handler);
  }

  window.BoyiDateRange = { quickRange, bindMonthWheel };
})();
