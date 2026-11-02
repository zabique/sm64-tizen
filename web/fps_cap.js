/* Optional frame-rate cap, OFF by default (page URL ?fps=30 turns it on).
   As a default it held 30 fps and halved CPU on a QN85A, but the game felt worse than uncapped
   (~40 fps there), so it is opt-in only.
   Each requestAnimationFrame() call still schedules exactly one callback, and the callback still
   runs once. Only the *rate* is reduced, by re-arming with the ORIGINAL rAF while waiting for the
   next slot. (Re-arming with the patched function double-schedules and blacks the canvas.)
   On the TV this took the page from ~48 % to ~25 % of a core. */
(function () {
  var m = /[?&]fps=(\d+)/.exec(location.search);
  var fps = m ? parseInt(m[1], 10) : 0;
  if (!fps) return;
  var nativeRaf = (window.requestAnimationFrame || window.webkitRequestAnimationFrame).bind(window);
  var interval = 1000 / fps, tol = 4, last = -1, next = 0;
  var counter = window.__sm64fps = { frames: 0 };
  /* The decision is per frame, not per callback: every callback of one frame gets the same
     timestamp, so all of them run in an allowed frame. (Gating per callback let the FPS meter's
     callback take the frame and starve the game's one -> black screen, no sound.)
     Slots are on a fixed grid: a frame that lands a vsync late does not push later slots back
     (that alone held a 60 Hz TV at ~27 fps). After a long stall the grid restarts. */
  function allowed(t) {
    if (t === last) return true;
    if (t < next - tol) return false;
    last = t;
    next = t - next > interval ? t + interval : next + interval;
    counter.frames++;
    return true;
  }
  function patched(cb) {
    return nativeRaf(function step(t) {
      if (!allowed(t)) { nativeRaf(step); return; }
      cb(t);
    });
  }
  window.requestAnimationFrame = patched;
  window.webkitRequestAnimationFrame = patched;
})();
