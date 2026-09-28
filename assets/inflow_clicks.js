/* Click-to-select wavelength on the live spectrum.

   The spectrum panel remounts as a brand-new Plotly component for every
   measured frame, so Plotly's own clickData can die with the old component
   when a click lands near a remount. This delegated listener lives on the
   stable slot container instead: it converts the click position straight to
   a wavelength and writes the analysis input - every click counts, no
   server round-trip. */
(function () {
  function handler(ev) {
    var slot = document.getElementById("spectrum-slot");
    if (!slot || !slot.contains(ev.target)) return;
    if (ev.target.closest && ev.target.closest(".modebar")) return;
    var gd = slot.querySelector(".js-plotly-plot");
    if (!gd || !gd._fullLayout || !gd._fullLayout.xaxis) return;
    var xa = gd._fullLayout.xaxis;
    if (!xa.p2d) return;
    var rect = gd.getBoundingClientRect();
    var xpx = ev.clientX - rect.left - xa._offset;
    if (xpx < 0 || xpx > xa._length) return; // outside the plot area
    var nm = xa.p2d(xpx);
    if (!isFinite(nm)) return;
    nm = Math.min(950, Math.max(380, Math.round(nm * 2) / 2));
    var input = document.getElementById("wavelength-input");
    if (!input) return;
    var setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value"
    ).set;
    setter.call(input, String(nm));
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
  // Capture phase: runs even if Plotly stops propagation internally.
  document.addEventListener("click", handler, true);
})();
