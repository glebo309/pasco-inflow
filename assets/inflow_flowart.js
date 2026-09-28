/* Sidebar ASCII flow cell. Pure decoration: animates only while the header
   status badge says a run is recording; costs the server nothing. */
(function () {
  var STREAM = "~~●~~~~○~~≈~~~●~~~~~○~~≈~~~~●~~○~~~";
  var SPARK = "▁▂▃▅▆▇▆▅▃▂";
  var BEAM = ["┊", "┆", "│"];
  var tick = 0;

  function rot(s, n) {
    n = ((n % s.length) + s.length) % s.length;
    return s.slice(n) + s.slice(0, n);
  }

  function beamChar(offset) {
    return BEAM[(tick + offset) % BEAM.length];
  }

  function runningFrame() {
    var flow = rot(STREAM, -tick); // sample moves left -> right
    var left = flow.slice(0, 3);
    var cell = flow.slice(3, 18);
    var right = flow.slice(18, 21);
    var pulse = tick % 3 === 0 ? "▼" : beamChar(1);
    var spark = rot(SPARK, tick);
    return [
      "           h·ν",
      "        " + beamChar(0) + "   " + beamChar(1) + "   " + beamChar(2),
      "   ┌────┴───┴───┴────┐",
      left + "┤" + cell + "├" + right,
      "   └────┬───┬───┬────┘",
      "        " + beamChar(2) + "   " + pulse + "   " + beamChar(0),
      "      [ detector ]",
      "       A " + spark.slice(0, 10),
    ].join("\n");
  }

  var IDLE = [
    "",
    "",
    "   ┌───────────────┐",
    "───┤    no flow    ├───",
    "   └───────────────┘",
    "",
    "      [ detector ]",
    "       A ▁▁▁▁▁▁▁▁▁▁",
  ].join("\n");

  function running() {
    var badge = document.querySelector(".inflow-run-status");
    return !!badge && badge.classList.contains("inflow-status-running");
  }

  setInterval(function () {
    var el = document.getElementById("flow-art");
    if (!el) return;
    if (running()) {
      tick += 1;
      el.textContent = runningFrame();
      el.classList.add("inflow-art-live");
    } else {
      el.textContent = IDLE;
      el.classList.remove("inflow-art-live");
    }
  }, 140);
})();
