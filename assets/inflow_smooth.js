/* Butter-smooth continuous motion.

   The server ships one small payload per measured spectrum; this engine
   interpolates between the two newest real frames at display refresh rate:
   - time chart: line tip + readhead glide (traces "time-line"/"time-readhead")
   - isoabsorbance map: the "map-now" line glides across the leading edge and
     the time axis grows with it (shape addressed by name, so server re-renders
     can never orphan it)
   Hidden graphs are skipped; the engine idles when an animation completes so
   it never fights a server render. */
(function () {
  var state = {
    frames: null,
    started: false,
    lastDraw: 0,
    lastMapDraw: 0,
    lastRight: null,
    mapBase: null,
    drawnHead: null,
    drawnSpec: null,
    drawnMap: null,
  };

  function plotDiv(id) {
    var host = document.getElementById(id);
    if (!host || host.offsetParent === null) return null; // absent or hidden
    return host.querySelector(".js-plotly-plot");
  }

  function traceIndex(gd, uidPrefix) {
    if (!gd || !gd.data) return -1;
    for (var i = 0; i < gd.data.length; i++) {
      var uid = gd.data[i].uid;
      if (uid && String(uid).indexOf(uidPrefix) === 0) return i;
    }
    return -1;
  }

  function shapeIndex(gd, name) {
    var shapes = gd && gd.layout && gd.layout.shapes;
    if (!shapes) return -1;
    for (var i = 0; i < shapes.length; i++) {
      if (shapes[i].name === name) return i;
    }
    return -1;
  }

  function lerp(a, b, frac) {
    return a === null || b === null || a === undefined || b === undefined
      ? null
      : a + (b - a) * frac;
  }

  function tick(now) {
    requestAnimationFrame(tick);
    var f = state.frames;
    if (!f || f.finished || !window.Plotly) return;
    if (now - state.lastDraw < 15) return; // full display rate (~60 fps)
    state.lastDraw = now;

    var frac = (Date.now() / 1000 - f.received_s) / f.interval;
    frac = Math.max(0, Math.min(1, frac));

    try {
      var sg = plotDiv("spectrum-slot");
      if (sg && f.spec_prev && f.spec_next) {
        var live = traceIndex(sg, "spectrum-live");
        if (live >= 0) {
          var count = Math.min(f.spec_prev.length, f.spec_next.length);
          var sy = new Array(count);
          for (var si = 0; si < count; si++) {
            sy[si] = lerp(f.spec_prev[si], f.spec_next[si], frac);
          }
          // Restyle only - the mount carries a y-range wide enough for both
          // frames, so no per-tick relayout (a full replot) is ever needed.
          Plotly.restyle(sg, { y: [sy] }, [live]);
          state.drawnSpec = sy;
          // The trail is the departed frame fading out on the same sample
          // clock as the glide.
          var ghost = traceIndex(sg, "spectrum-ghost");
          if (ghost >= 0) {
            Plotly.restyle(sg, { opacity: 0.4 * (1 - frac) }, [ghost]);
          }
        }
      }

      var tg = plotDiv("time-graph");
      if (tg && f.head_prev && f.head_next) {
        var line = traceIndex(tg, "time-line");
        var head = traceIndex(tg, "time-readhead");
        var hx = lerp(f.head_prev[0], f.head_next[0], frac);
        var hy = lerp(f.head_prev[1], f.head_next[1], frac);
        if (hx !== null && hy !== null) state.drawnHead = [hx, hy];
        if (line >= 0 && hx !== null && hy !== null) {
          var lx = f.line_x.concat([hx]);
          var ly = f.line_y.concat([hy]);
          if (head >= 0) {
            Plotly.restyle(tg, { x: [lx, [hx]], y: [ly, [hy]] }, [line, head]);
          } else {
            Plotly.restyle(tg, { x: [lx], y: [ly] }, [line]);
          }
        }
      }

      var mg = plotDiv("surface-graph");
      if (mg && f.head_prev && f.head_next) {
        var tNow = lerp(f.head_prev[0], f.head_next[0], frac);
        var idx = shapeIndex(mg, "map-now");
        if (tNow !== null && idx >= 0) {
          var update = {};
          update["shapes[" + idx + "].x0"] = tNow;
          update["shapes[" + idx + "].x1"] = tNow;
          var axis = mg.layout.xaxis;
          var range = axis && axis.range ? axis.range : null;
          if (range) {
            if (state.lastRight === null) state.lastRight = range[1];
            // Only grow the window while the user has not zoomed elsewhere.
            if (Math.abs(range[1] - state.lastRight) < 1e-6 && tNow > range[1]) {
              update["xaxis.range"] = [range[0], tNow];
              state.lastRight = tNow;
            }
          }
          Plotly.relayout(mg, update);
        }
        // Grow the leading column exactly like the gliding A(t) tip: the
        // server holds back the newest spectrum's column, and this appends it
        // at the moving edge with colors morphing from the previous spectrum
        // into the latest one over the measurement interval. Heatmap restyles
        // are heavier than line restyles, so cap them at ~15 fps.
        if (f.map_prev && f.map_next && mg.data && mg.data[0] && mg.data[0].z && tNow !== null) {
          if (!state.mapBase && mg.data[0].z.length === f.map_prev.length) {
            var baseRows = [];
            for (var b = 0; b < mg.data[0].z.length; b++) {
              baseRows.push(Array.prototype.slice.call(mg.data[0].z[b]));
            }
            state.mapBase = {
              x: Array.prototype.slice.call(mg.data[0].x),
              z: baseRows,
            };
          }
          if (state.mapBase && now - state.lastMapDraw >= 66) {
            state.lastMapDraw = now;
            var grownX = state.mapBase.x.concat([tNow]);
            var grownZ = [];
            var drawnColumn = new Array(state.mapBase.z.length);
            for (var r = 0; r < state.mapBase.z.length; r++) {
              var row = state.mapBase.z[r];
              var value = lerp(f.map_prev[r], f.map_next[r], frac);
              drawnColumn[r] = value;
              grownZ.push(row.concat([value === null ? row[row.length - 1] : value]));
            }
            state.drawnMap = drawnColumn;
            Plotly.restyle(mg, { x: [grownX], z: [grownZ] }, [0]);
          }
        }
      }

      if (frac >= 1) f.finished = true; // idle until the next payload arrives
    } catch (err) {
      /* never break the page over a dropped animation frame */
    }
  }

  window.dash_clientside = Object.assign({}, window.dash_clientside, {
    inflow: {
      register_frames: function (data) {
        if (data) {
          var sg = plotDiv("spectrum-slot");
          if (sg && data.spectrum_figure && window.Plotly) {
            Plotly.react(
              sg,
              data.spectrum_figure.data || [],
              data.spectrum_figure.layout || {},
              data.spectrum_config || sg._context || {}
            );
          }
          // Hand-off continuity: if the previous glide had not finished when
          // this packet arrived (poll jitter), start the new glide from the
          // exact position last drawn instead of snapping to the official
          // previous point.
          if (data.head_prev && data.head_next && state.drawnHead) {
            var lastX = state.drawnHead[0];
            var inSequence =
              lastX >= data.head_prev[0] - data.interval * 2 &&
              lastX <= data.head_next[0];
            if (inSequence) {
              if (lastX > data.head_prev[0]) data.head_prev = state.drawnHead;
              if (
                state.drawnSpec && data.spec_prev && data.spec_next &&
                state.drawnSpec.length === data.spec_next.length
              ) {
                data.spec_prev = state.drawnSpec;
              }
              if (
                state.drawnMap && data.map_prev && data.map_next &&
                state.drawnMap.length === data.map_next.length
              ) {
                data.map_prev = state.drawnMap;
              }
            } else {
              state.drawnHead = null;
              state.drawnSpec = null;
              state.drawnMap = null;
            }
          }
          data.received_s = Date.now() / 1000;
          data.finished = !(data.head_prev && data.head_next);
        } else {
          state.drawnHead = null;
          state.drawnSpec = null;
          state.drawnMap = null;
        }
        state.frames = data;
        state.lastRight = null;
        state.mapBase = null; // re-snapshot from the freshly mounted map
        if (!state.started) {
          state.started = true;
          requestAnimationFrame(tick);
        }
        return "";
      },
    },
  });
})();
