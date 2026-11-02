/* Gamepad fix: the game reads pad 0 only.
   After the Xbox pad reconnects (e.g. it slept), the TV's gamepad-service can leave the old
   virtual pad behind, and the page then lists the controller twice: a dead "Xbox Wireless
   Controller" at index 0 and the live one at index 1 -> the game ignores every press.
   navigator.getGamepads() is wrapped so the pad that most recently changed state comes first.
   Page URL ?padfix=0 disables it. */
(function () {
  if (!navigator.getGamepads || /[?&]padfix=0/.test(location.search)) return;
  var native = navigator.getGamepads.bind(navigator);
  var active = -1, lastTs = [];
  function track(list) {
    for (var i = 0; i < list.length; i++) {
      var g = list[i];
      if (!g) continue;
      if (lastTs[i] !== undefined && g.timestamp !== lastTs[i]) active = i;
      lastTs[i] = g.timestamp;
    }
  }
  navigator.getGamepads = function () {
    var list = native();
    track(list);
    if (active <= 0 || !list[active]) return list;
    var out = [list[active]];
    for (var i = 0; i < list.length; i++) if (i !== active) out.push(list[i]);
    return out;
  };
  window.__sm64pads = { native: native, active: function () { return active; } };
})();
