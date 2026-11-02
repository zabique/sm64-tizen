/* Audio stutter fix for the Emscripten SDL2 build (wraps SDL's audio node at runtime).

   SDL2's Emscripten backend plays through createScriptProcessor(<SDL samples>, here 512) and
   refills it from onaudioprocess on the main thread. On the QN85A the context runs at 44.1 kHz,
   so that node needs 86 refills a second, each within ~11 ms, while a game frame blocks the main
   thread for 25+ ms. Measured: only ~75 of 86 refills/s arrived and 3-8/s came late -> stutter.

   Fix: SDL keeps its 512-frame "node", but it is a fake. SDL's own onaudioprocess handler is
   called with fake events whose outputBuffer is a 512-frame window into our buffer, paced by the
   audio clock (so SDL's queue drains at the same rate as before and the game's buffer feedback is
   unchanged). What actually plays it, best first:
     worklet - an AudioWorklet consumes the pulled audio on the audio thread; latency = lead.
     ring    - a large ScriptProcessor (ab frames) copies out of a ring buffer. Chromium
               double-buffers it, so latency = lead + ab. ab=2048 still dropped a buffer every
               ~2 s on the TV; ab=4096 dropped none over 90 s.
     native  - SDL's own node, only instrumented (ab=0, for A/B tests).

   Query parameters (on the page URL):
     aw=0          do not try the AudioWorklet
     ab=<frames>   ScriptProcessor size for the ring path (default 4096); ab=0 = native path
     lead=<frames> how far the pulled audio runs ahead of the audio clock
     log=<url>     send stats once a second to <url>/log?... (image beacon, no CORS needed) */
(function () {
  var AC = window.AudioContext || window.webkitAudioContext;
  if (!AC || !AC.prototype.createScriptProcessor) return;

  function param(name, def) {
    var m = new RegExp('[?&]' + name + '=([^&]*)').exec(location.search);
    return m ? decodeURIComponent(m[1]) : def;
  }
  var USE_AW = param('aw', '1') !== '0' && typeof AudioWorkletNode === 'function';
  var BIG = parseInt(param('ab', '4096'), 10);
  var LEAD_PARAM = param('lead', '');
  var LOG = param('log', '');

  var stats = window.__sm64audio = {
    mode: 'none', rate: 0, sdl: 0, big: BIG, lead: 0, events: 0, late: 0, minHead: 1e9,
    short: 0, resync: 0, pulls: 0, level: 0, under: 0, err: ''
  };

  var spDesc = Object.getOwnPropertyDescriptor(ScriptProcessorNode.prototype, 'onaudioprocess');
  var orig = AC.prototype.createScriptProcessor;

  /* ---- AudioWorklet: needs its module loaded before SDL opens the device, so create the
     context now and hand it to SDL's `new AudioContext()` later. ---- */
  var WORKLET = [
    'class Sm64Out extends AudioWorkletProcessor {',
    '  constructor(o) {',
    '    super(); this.ch = o.processorOptions.ch; this.max = o.processorOptions.max;',
    '    this.q = []; this.off = 0; this.level = 0; this.debt = 0; this.under = 0; this.drop = 0; this.n = 0;',
    '    this.port.onmessage = (e) => {',
    '      if (e.data.reset) { this.debt = 0; return; }',
    '      this.q.push(e.data); this.level += e.data.length / this.ch; this.started = true;',
    '    };',
    '  }',
    '  skip(frames) {',
    '    while (frames > 0 && this.q.length) {',
    '      var len = this.q[0].length / this.ch, k = Math.min(frames, len - this.off);',
    '      this.off += k; this.level -= k; frames -= k;',
    '      if (this.off >= len) { this.q.shift(); this.off = 0; }',
    '    }',
    '  }',
    '  process(inputs, outputs) {',
    '    var out = outputs[0], n = out[0].length, i = 0;',
    /* frames that went silent while starved are skipped once data arrives (stay in sync) */
    '    if (this.debt && this.level) { var k = Math.min(this.debt, this.level); this.skip(k); this.debt -= k; }',
    '    if (this.level > this.max) { this.drop += this.level - this.max; this.skip(this.level - this.max); }',
    '    while (i < n && this.q.length) {',
    '      var buf = this.q[0], len = buf.length / this.ch, take = Math.min(n - i, len - this.off);',
    '      for (var c = 0; c < out.length; c++) {',
    '        var s = (c < this.ch ? c : 0) * len + this.off;',
    '        out[c].set(buf.subarray(s, s + take), i);',
    '      }',
    '      i += take; this.off += take; this.level -= take;',
    '      if (this.off >= len) { this.q.shift(); this.off = 0; }',
    '    }',
    '    if (i < n) {',
    '      for (var z = 0; z < out.length; z++) out[z].fill(0, i);',
    /* debt only counts once audio flows (the pulls are paced from then on), and is capped */
    '      if (this.started) { this.under += n - i; this.debt = Math.min(this.debt + n - i, this.max); }',
    '    }',
    '    if (++this.n % 256 === 0) this.port.postMessage({ level: this.level, under: this.under, drop: this.drop });',
    '    return true;',
    '  }',
    '}',
    'registerProcessor("sm64-out", Sm64Out);'
  ].join('\n');

  var pre = null, awReady = false;
  if (USE_AW) {
    try {
      pre = new AC();
      var url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
      pre.audioWorklet.addModule(url).then(function () { awReady = true; },
        function (err) { stats.err = 'addModule: ' + err; });
      var Patched = function () { if (pre && !Patched.used) { Patched.used = true; return pre; } return new AC(); };
      Patched.prototype = AC.prototype;
      if (window.AudioContext) window.AudioContext = Patched;
      if (window.webkitAudioContext) window.webkitAudioContext = Patched;
    } catch (err) { stats.err = 'worklet: ' + err; pre = null; }
  }

  /* headroom = how long before this buffer is due the main thread got to run the handler */
  function noteEvent(ctx, e) {
    stats.events++;
    if (typeof e.playbackTime === 'number' && e.playbackTime > 0) {
      var head = (e.playbackTime - ctx.currentTime) * 1000;
      if (head < stats.minHead) stats.minHead = head;
      if (head < 0) stats.late++;
    }
  }

  /* SDL's handler, called with a fake event whose channels are B-frame windows of `buf`
     (planar: channel c of the chunk at frame `at` starts at c * stride + at) */
  function makePuller(ctx, B, CH) {
    var self = { handler: null, buf: null, stride: 0, at: 0, node: null };
    var fakeBuf = {
      numberOfChannels: CH, length: B, sampleRate: ctx.sampleRate, duration: B / ctx.sampleRate,
      getChannelData: function (c) { var s = c * self.stride + self.at; return self.buf.subarray(s, s + B); }
    };
    var fakeEvt = { outputBuffer: fakeBuf, inputBuffer: null, playbackTime: 0 };
    self.pull = function (buf, stride, at) {
      self.buf = buf; self.stride = stride; self.at = at;
      for (var c = 0; c < CH; c++) buf.fill(0, c * stride + at, c * stride + at + B);
      try { self.handler.call(self.node, fakeEvt); } catch (err) { stats.err = String(err); }
      stats.pulls++;
    };
    return self;
  }

  function capture(obj, puller) {
    Object.defineProperty(obj, 'onaudioprocess', {
      configurable: true,
      get: function () { return puller.handler; },
      set: function (fn) { puller.handler = typeof fn === 'function' ? fn : null; }
    });
  }

  function workletPath(ctx, B, CH) {
    var LEAD = parseInt(LEAD_PARAM || '4096', 10);
    stats.mode = 'worklet'; stats.lead = LEAD; stats.big = 0;
    var node = new AudioWorkletNode(ctx, 'sm64-out', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [CH],
      processorOptions: { ch: CH, max: LEAD * 3 }
    });
    node.port.onmessage = function (e) { stats.level = e.data.level; stats.under = e.data.under; stats.resync = e.data.drop; };
    var P = makePuller(ctx, B, CH), wr = 0, t0 = -1, timer = 0;
    var fake = {
      connect: function () { return node.connect.apply(node, arguments); },
      disconnect: function () {
        P.handler = null; if (timer) { clearInterval(timer); timer = 0; }
        return node.disconnect.apply(node, arguments);
      }
    };
    P.node = fake;
    capture(fake, P);
    /* pull everything due by the audio clock (+ LEAD) in one message to the worklet */
    timer = setInterval(function () {
      if (!P.handler) return;
      if (t0 < 0) { if (ctx.currentTime <= 0) return; t0 = ctx.currentTime; }
      var target = Math.floor((ctx.currentTime - t0) * ctx.sampleRate) + LEAD;
      if (target - wr > ctx.sampleRate) {   /* stalled over a second: start over, forget the gap */
        wr = target - LEAD; stats.short++; node.port.postMessage({ reset: true });
      }
      var chunks = Math.floor((target - wr) / B);
      if (chunks <= 0) return;
      var stride = chunks * B, buf = new Float32Array(stride * CH);
      for (var k = 0; k < chunks; k++) P.pull(buf, stride, k * B);
      wr += stride;
      node.port.postMessage(buf, [buf.buffer]);
    }, 4);
    return fake;
  }

  function ringPath(ctx, B, CH) {
    var LEAD = parseInt(LEAD_PARAM || String(BIG + (BIG >> 1)), 10);
    stats.mode = 'ring'; stats.lead = LEAD;
    var RING = BIG * 8;   /* multiple of B, so a chunk never wraps */
    var node = orig.call(ctx, BIG, 0, CH);
    var ring = new Float32Array(RING * CH);
    var P = makePuller(ctx, B, CH), wr = 0, rd = 0, t0 = -1, timer = 0;
    P.node = node;

    /* keep the pulled audio LEAD frames ahead of the audio clock (and at least `need`) */
    function pump(need) {
      if (!P.handler || t0 < 0) return;
      var target = Math.floor((ctx.currentTime - t0) * ctx.sampleRate) + LEAD;
      if (need > target) target = need;
      while (wr < target && wr - rd + B <= RING) { P.pull(ring, RING, wr % RING); wr += B; }
    }

    /* Chromium holds the node's lock while this handler runs; a buffer boundary that hits it
       is played as silence and its event is never fired. So: copy only, pull elsewhere. */
    spDesc.set.call(node, function (e) {
      noteEvent(ctx, e);
      if (!P.handler) return;
      var out = e.outputBuffer, n = out.length;
      var now = ctx.currentTime;
      if (t0 < 0) { t0 = now; wr = rd = 0; }
      /* t0 = audio-clock time of ring frame 0; dispatch is only ever late, so take the minimum */
      if (now - rd / ctx.sampleRate < t0) t0 = now - rd / ctx.sampleRate;
      /* a whole buffer behind the clock = Chromium dropped an event (played silence there):
         skip that buffer's audio so the ring stays in sync instead of piling up latency */
      var behind = Math.floor((now - t0) * ctx.sampleRate) - rd;
      while (behind >= n) { rd += n; behind -= n; stats.resync++; }
      if (wr - rd < n) pump(rd + n);
      if (wr - rd < n) stats.short++;
      var pos = rd % RING, first = Math.min(n, RING - pos);
      for (var c = 0; c < CH; c++) {
        var dst = out.getChannelData(c);
        dst.set(ring.subarray(c * RING + pos, c * RING + pos + first), 0);
        if (first < n) dst.set(ring.subarray(c * RING, c * RING + n - first), first);
      }
      rd += n;
      stats.level = wr - rd;
    });

    capture(node, P);
    var nativeDisconnect = node.disconnect;
    node.disconnect = function () {
      P.handler = null;
      if (timer) { clearInterval(timer); timer = 0; }
      return nativeDisconnect.apply(node, arguments);
    };
    /* small, frequent pulls between frames, like the original 512-frame node did */
    timer = setInterval(function () { pump(0); }, 4);
    return node;
  }

  function nativePath(ctx, args) {
    stats.mode = 'native';
    var node = orig.apply(ctx, args);
    Object.defineProperty(node, 'onaudioprocess', {
      configurable: true,
      get: function () { return spDesc.get.call(node); },
      set: function (fn) {
        spDesc.set.call(node, typeof fn === 'function'
          ? function (e) { noteEvent(ctx, e); return fn.call(this, e); } : fn);
      }
    });
    return node;
  }

  AC.prototype.createScriptProcessor = function (size, inCh, outCh) {
    if (inCh || !outCh) return orig.apply(this, arguments);   /* capture: leave alone */
    stats.rate = this.sampleRate; stats.sdl = size;
    try {
      if (this === pre && awReady) return workletPath(this, size, outCh);
    } catch (err) { stats.err = 'worklet path: ' + err; }
    if (BIG > size) return ringPath(this, size, outCh);
    return nativePath(this, arguments);
  };

  if (LOG) {
    setInterval(function () {
      var q = [];
      if (window.__sm64fps) { stats.fps = window.__sm64fps.frames; window.__sm64fps.frames = 0; }   /* fps_cap.js */
      /* input diagnostics: which pads the page sees, buttons held right now, focus */
      try {
        /* raw browser list (pad_fix.js reorders what the game sees): index:held:timestamp:id */
        var get = window.__sm64pads ? window.__sm64pads.native
                : navigator.getGamepads ? navigator.getGamepads.bind(navigator) : function () { return []; };
        var gp = get(), ids = [], held = 0;
        for (var i = 0; i < gp.length; i++) {
          if (!gp[i]) continue;
          var h = 0;
          for (var b = 0; b < gp[i].buttons.length; b++) if (gp[i].buttons[b].pressed) h++;
          held += h;
          ids.push(i + ':' + h + ':' + Math.round(gp[i].timestamp) + ':' + gp[i].id.slice(0, 24) + (gp[i].connected ? '' : '(off)'));
        }
        stats.pads = ids.join('|') || 'none'; stats.held = held;
        stats.active = window.__sm64pads ? window.__sm64pads.active() : 'nofix';
        stats.focus = (document.hasFocus() ? 'y' : 'n') + document.visibilityState.charAt(0);
      } catch (err) { stats.pads = 'err ' + err; }
      for (var k in stats) q.push(k + '=' + encodeURIComponent(stats[k]));
      new Image().src = LOG.replace(/\/$/, '') + '/log?' + q.join('&') + '&t=' + Date.now();
      stats.late = 0; stats.minHead = 1e9; stats.events = 0;
    }, 1000);
  }
})();
