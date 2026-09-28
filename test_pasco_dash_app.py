from pathlib import Path
import os

import pytest

from pasco_dash_app import refresh_run_choices, spectrum_figure


def test_instrument_strip_distributes_all_readouts_across_the_panel():
    css = (Path(__file__).parent / "assets" / "inflow.css").read_text()
    strip_block = css.split("#inflow-app .inflow-instrument-strip {", 1)[1].split("}", 1)[0]
    display_block = css.split("#inflow-app .inflow-display-control {", 1)[1].split("}", 1)[0]

    assert "justify-content: space-between" in strip_block
    assert "flex: 0 0 auto" in display_block


def test_ascii_detector_bottom_aligns_with_audit_terminal():
    css = (Path(__file__).parent / "assets" / "inflow.css").read_text()
    art_block = css.split("#inflow-app .inflow-art {", 1)[1].split("}", 1)[0]

    # The black terminal ends 15 px above the shell bottom: 8 px main padding,
    # 1 px card border, and 6 px details-body padding.
    assert "margin: auto 0 15px" in art_block
    assert "padding: 16px 8px 0" in art_block
    assert "font: 12px/1.4 var(--mono)" in art_block


def write_pasco_csv(path: Path, run_number: int, absorbance):
    path.write_text(
        "Wavelength (nm),Absorbance Solution #"
        f"{run_number},Transmittance Solution #{run_number}\n"
        f"400.0,{absorbance},90.0\n"
        f"500.0,{absorbance},80.0\n"
        f"600.0,{absorbance},70.0\n"
    )


def test_continuous_full_spectrum_is_a_static_scaffold_for_client_animation(tmp_path):
    # Continuous motion is animated in the BROWSER (assets/inflow_smooth.js):
    # the server sends this figure once as a scaffold with uid-addressable
    # traces, then ships per-measurement payloads that the client interpolates
    # at display refresh rate. The figure must therefore be time-independent -
    # re-rendering it per frame fights the animation engine and freezes the
    # display (seen live on 2026-08-20).
    previous = tmp_path / "inline_00001_t0000.000s.csv"
    latest = tmp_path / "inline_00002_t0002.000s.csv"
    write_pasco_csv(previous, 1, 1.0)
    write_pasco_csv(latest, 2, 3.0)
    os.utime(latest, (100.0, 100.0))

    early = spectrum_figure(
        [previous, latest],
        500.0,
        smooth=True,
        interval_s=2.0,
        now_epoch=100.5,
    )
    late = spectrum_figure(
        [previous, latest],
        500.0,
        smooth=True,
        interval_s=2.0,
        now_epoch=101.5,
    )

    assert str(early.data[-1].uid).startswith("spectrum-live-")
    assert str(late.data[-1].uid).startswith("spectrum-live-")
    ghosts = [trace for trace in early.data if str(trace.uid).startswith("spectrum-ghost-")]
    assert len(ghosts) == 1
    assert list(ghosts[0].y) == pytest.approx([1.0, 1.0, 1.0])
    assert list(early.data[-1].y) == pytest.approx([3.0, 3.0, 3.0])
    # Time-independent: identical scaffold regardless of when it is rendered.
    assert list(late.data[-1].y) == list(early.data[-1].y)
    assert late.layout.meta["displayed_elapsed_s"] == early.layout.meta["displayed_elapsed_s"]


def test_continuous_full_spectrum_uses_a_new_plotly_trace_identity_for_each_csv(tmp_path):
    """Plotly must not reconcile a new spectrum onto the previous frozen trace."""
    first = tmp_path / "inline_00001_t0000.000s.csv"
    second = tmp_path / "inline_00002_t0002.000s.csv"
    third = tmp_path / "inline_00003_t0004.000s.csv"
    write_pasco_csv(first, 1, 0.1)
    write_pasco_csv(second, 2, 0.2)
    write_pasco_csv(third, 3, 0.3)

    before = spectrum_figure([first, second], 500.0, smooth=True)
    after = spectrum_figure([first, second, third], 500.0, smooth=True)

    before_live = next(trace for trace in before.data if str(trace.uid).startswith("spectrum-live"))
    after_live = next(trace for trace in after.data if str(trace.uid).startswith("spectrum-live"))
    assert before_live.uid != after_live.uid
    assert second.name in before_live.uid
    assert third.name in after_live.uid


def test_chopped_full_spectrum_shows_only_latest_and_scales_to_it(tmp_path):
    saturated = tmp_path / "inline_00001_t0000.000s.csv"
    latest = tmp_path / "inline_00002_t0002.000s.csv"
    write_pasco_csv(saturated, 1, 3.0)
    write_pasco_csv(latest, 2, 0.1)

    figure = spectrum_figure([saturated, latest], 500.0, smooth=False)

    assert len(figure.data) == 2  # click-catcher columns + latest spectrum
    assert figure.data[0].uid == "wavelength-click-catcher"
    assert figure.data[0].marker.color != "rgba(0,0,0,0)"
    assert str(figure.data[-1].uid).endswith("t0002.000s.csv")
    assert str(figure.layout.datarevision).endswith("inline_00002_t0002.000s.csv")
    assert figure.layout.yaxis.range[1] < 0.2


def test_same_csv_filename_in_two_runs_has_distinct_plotly_identity(tmp_path):
    run_a = tmp_path / "run-a" / "exports"
    run_b = tmp_path / "run-b" / "exports"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)
    name = "inline_00001_t0000.500s.csv"
    first = run_a / name
    second = run_b / name
    write_pasco_csv(first, 1, 0.1)
    write_pasco_csv(second, 1, 0.9)

    figure_a = spectrum_figure([first], 500.0, smooth=False)
    figure_b = spectrum_figure([second], 500.0, smooth=False)

    assert figure_a.data[-1].uid != figure_b.data[-1].uid
    assert figure_a.layout.datarevision != figure_b.layout.datarevision


def test_selected_run_spectrum_is_resent_even_if_browser_claims_it_rendered(tmp_path, monkeypatch):
    import json
    from dash import no_update
    import pasco_dash_app

    monkeypatch.setattr(pasco_dash_app, "discover_active_run", lambda *_args, **_kwargs: None)
    run_dir = tmp_path / "run-a"
    exports = run_dir / "exports"
    exports.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"state": "stopped", "spectra_captured": 1}))
    latest = exports / "inline_00001_t0000.500s.csv"
    write_pasco_csv(latest, 1, 0.9)
    claimed_signature = {
        "time": [run_dir.name, latest.name, 1, 520.0],
        "spectrum": [run_dir.name, latest.name, 1, 520.0],
    }

    result = pasco_dash_app.refresh_dashboard(
        0,
        run_dir.name,
        520,
        [],
        "2d",
        {"path": str(tmp_path)},
        claimed_signature,
    )

    assert result[9] is not no_update
    assert result[9].id["type"] == "spectrum-frame"
    assert list(result[9].figure.data[-1].y) == pytest.approx([0.9, 0.9, 0.9])


def test_choosing_a_save_folder_does_not_auto_load_an_old_run(tmp_path):
    old_run = tmp_path / "20260820_120000"
    old_run.mkdir()
    (old_run / "status.json").write_text('{"state": "complete", "spectra_captured": 4}')

    choices, selected = refresh_run_choices(
        0,
        None,
        {"path": str(tmp_path), "confirmed": True},
        None,
    )

    assert [choice["value"] for choice in choices] == [old_run.name]
    assert selected is None


def test_mid_write_spectrum_file_skips_the_frame_instead_of_failing(tmp_path, monkeypatch):
    import json

    from dash import no_update

    import pasco_dash_app

    monkeypatch.setattr(pasco_dash_app, "discover_active_run", lambda *_args, **_kwargs: None)
    refresh_dashboard = pasco_dash_app.refresh_dashboard

    run_dir = tmp_path / "20260820_130000"
    exports = run_dir / "exports"
    exports.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"state": "running", "spectra_captured": 2}))
    write_pasco_csv(exports / "inline_00001_t0000.500s.csv", 1, 0.5)
    # Latest file caught mid-write: header only, no data rows yet.
    (exports / "inline_00002_t0001.500s.csv").write_text("Wavelength (nm),Absorbance Solution #2,Transmittance Solution #2\n")

    result = refresh_dashboard(0, run_dir.name, 520, [], "2d", {"path": str(tmp_path)}, None)

    assert result[8] is no_update  # time figure untouched
    assert result[9] is no_update  # spectrum figure untouched
    assert result[-2] is no_update  # render signature untouched, retry next poll


def test_smooth_animation_payload_interpolates_the_two_newest_frames(tmp_path):
    from pasco_dash_app import smooth_animation_payload

    first = tmp_path / "inline_00001_t0000.000s.csv"
    middle = tmp_path / "inline_00002_t0002.000s.csv"
    latest = tmp_path / "inline_00003_t0004.000s.csv"
    write_pasco_csv(first, 1, 1.0)
    write_pasco_csv(middle, 2, 2.0)
    write_pasco_csv(latest, 3, 3.0)

    payload = smooth_animation_payload([first, middle, latest], 500.0)

    assert payload["interval"] == pytest.approx(2.0)
    # Frames for the 2D spectrum morph (scaffold shows the previous spectrum,
    # the engine glides it into the latest - same contract as the A(t) tip).
    assert payload["spec_prev"] == pytest.approx([2.0, 2.0, 2.0])
    assert payload["spec_next"] == pytest.approx([3.0, 3.0, 3.0])
    assert payload["spec_range_prev"][1] > 2.0
    assert payload["spec_range_next"][1] == pytest.approx(3.0)  # capped at A=3
    assert payload["head_prev"] == pytest.approx([2.0, 2.0])
    assert payload["head_next"] == pytest.approx([4.0, 3.0])
    assert payload["line_x"] == pytest.approx([0.0, 2.0])

    assert smooth_animation_payload([first], 500.0) is None


def test_continuous_mode_mounts_previous_spectrum_and_ships_morph_frames(tmp_path, monkeypatch):
    import json

    from dash import no_update
    import pasco_dash_app

    monkeypatch.setattr(pasco_dash_app, "discover_active_run", lambda *_args, **_kwargs: None)
    refresh_dashboard = pasco_dash_app.refresh_dashboard

    run_dir = tmp_path / "20260820_130000"
    exports = run_dir / "exports"
    exports.mkdir(parents=True)
    (run_dir / "status.json").write_text(
        json.dumps({"state": "running", "spectra_captured": 2, "interval_s": 2})
    )
    first = exports / "inline_00001_t0000.500s.csv"
    latest = exports / "inline_00002_t0002.500s.csv"
    write_pasco_csv(first, 1, 0.1)
    write_pasco_csv(latest, 2, 1.2)

    previous_client_state = {
        "figure": [run_dir.name, "smooth-scaffold", 520.0],
        "frames": [run_dir.name, first.name, 520.0],
    }
    result = refresh_dashboard(
        0,
        run_dir.name,
        520,
        ["smooth"],
        "2d",
        {"path": str(tmp_path)},
        previous_client_state,
    )

    assert result[9] is not no_update
    # One-measurement display delay (same contract as the A(t) tip and the
    # map edge): the scaffold mounts the PREVIOUS spectrum and the browser
    # engine morphs it into the latest, so every mount lands exactly where
    # the animation finished - no jump, no stale-trace fight.
    live_trace = result[9].figure.data[-1]
    assert live_trace.uid == "spectrum-live"
    assert list(live_trace.y) == pytest.approx([0.1, 0.1, 0.1])
    frames = result[-1]
    assert frames["spec_prev"] == pytest.approx([0.1, 0.1, 0.1])
    assert frames["spec_next"] == pytest.approx([1.2, 1.2, 1.2])


def test_stop_button_only_signals_the_worker_so_pasco_is_not_clicked_twice(tmp_path, monkeypatch):
    import types
    import pasco_dash_app

    run_dir = tmp_path / "20260820_130000"
    run_dir.mkdir()
    monkeypatch.setattr(pasco_dash_app, "ctx", types.SimpleNamespace(triggered_id="stop-button"))
    monkeypatch.setattr(pasco_dash_app, "discover_active_run", lambda *_args, **_kwargs: run_dir)
    monkeypatch.setattr(
        pasco_dash_app,
        "stop_recording_preserving_context",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("dashboard clicked PASCO")),
    )

    _action, message, _save = pasco_dash_app.acquisition_action(
        0,
        1,
        1,
        0,
        None,
        run_dir.name,
        2,
        10,
        [],
        {"path": str(tmp_path)},
    )

    assert (run_dir / "STOP").exists()
    assert message == "Stopping…"


def test_idle_state_clears_a_stale_spectrum_even_when_time_graph_is_already_idle():
    from dash import no_update
    from pasco_dash_app import refresh_dashboard

    result = refresh_dashboard(
        0,
        None,
        520,
        ["smooth"],
        "2d",
        {"path": "/tmp"},
        {"time": ["idle"], "spectrum": ["old-deleted-run"]},
    )

    assert result[8] is no_update
    assert result[9] is not no_update
    assert result[9].figure.layout.annotations[0].text == "The latest calibrated spectrum will appear here"
    assert result[-2]["spectrum"] == ["idle"]


def test_isoabsorbance_map_shows_absorbance_over_wavelength_and_time(tmp_path):
    from pasco_dash_app import isoabsorbance_figure

    names = ["inline_00001_t0000.000s.csv", "inline_00002_t0002.000s.csv", "inline_00003_t0004.000s.csv"]
    paths = []
    for i, name in enumerate(names, start=1):
        path = tmp_path / name
        write_pasco_csv(path, i, float(i))
        paths.append(path)

    figure = isoabsorbance_figure(paths, 500.0, wavelength_stride=1)

    heatmap = figure.data[0]
    assert heatmap.type == "heatmap"
    assert list(heatmap.x) == pytest.approx([0.0, 2.0, 4.0])  # time axis
    assert list(heatmap.y) == pytest.approx([400.0, 500.0, 600.0])  # wavelength axis
    assert [list(row) for row in heatmap.z] == [[1.0, 2.0, 3.0]] * 3
    # The analysis wavelength is marked directly on the map.
    assert figure.layout.shapes[0].y0 == pytest.approx(500.0)
    # The leading-edge "now" line is addressable by name so the browser engine
    # can glide it (and the time window) between measurements.
    now_line = next(shape for shape in figure.layout.shapes if shape.name == "map-now")
    assert now_line.x0 == pytest.approx(4.0)
    assert list(figure.layout.xaxis.range) == pytest.approx([0.0, 4.0])
    # Zoom must survive live updates while spectra append.
    assert figure.layout.uirevision == "isoabsorbance"


def test_isoabsorbance_map_uses_the_selected_absorbance_colour_window(tmp_path):
    from pasco_dash_app import isoabsorbance_figure

    path = tmp_path / "inline_00001_t0000.000s.csv"
    write_pasco_csv(path, 1, 0.5)

    figure = isoabsorbance_figure(
        [path],
        500.0,
        wavelength_stride=1,
        color_range=[0.1, 0.8],
        color_scale="ocean",
    )

    heatmap = figure.data[0]
    assert heatmap.zmin == pytest.approx(0.1)
    assert heatmap.zmax == pytest.approx(0.8)
    assert heatmap.colorscale[0][1] == "#f7fbff"
    assert heatmap.colorscale[-1][1] == "#172554"


def test_isoabsorbance_colour_controls_are_visible_and_vertical():
    from pasco_dash_app import app

    def components(node):
        yield node
        children = getattr(node, "children", None)
        if children is None:
            return
        for child in children if isinstance(children, (list, tuple)) else [children]:
            if hasattr(child, "children") or hasattr(child, "id"):
                yield from components(child)

    by_id = {getattr(node, "id", None): node for node in components(app.layout)}
    slider = by_id["map-color-range"]
    palette = by_id["map-color-scale"]
    ticks = by_id["map-color-ticks"]

    assert slider.vertical is True
    assert slider.value == [0.0, 0.8]
    assert slider.marks == {value: str(value) for value in range(4)}
    assert [tick.children for tick in ticks.children] == ["3", "2", "1", "0"]
    assert "map-color-legend-ticks" not in by_id
    assert palette.value == "spectrum"
    assert {option["value"] for option in palette.options} >= {"spectrum", "ocean", "inferno", "gray"}


def test_vertical_map_slider_uses_the_available_height_without_global_slider_css_collision():
    inflow_css = (Path(__file__).parent / "assets" / "inflow.css").read_text()
    shared_css = (Path(__file__).parent / "assets" / "style.css").read_text()
    range_block = inflow_css.split("#inflow-app .inflow-map-range {", 1)[1].split("}", 1)[0]

    assert "flex: 1 1 auto" in range_block
    assert "height: 100%" in inflow_css.split("#inflow-app #map-color-range {", 1)[1].split("}", 1)[0]
    gradient_block = inflow_css.split("#inflow-app .inflow-map-gradient {", 1)[1].split("}", 1)[0]
    ticks_block = inflow_css.split("#inflow-app .inflow-map-ticks {", 1)[1].split("}", 1)[0]
    assert "margin: 24px 0" in gradient_block
    assert "margin: 24px 0" in ticks_block
    assert "\n.rc-slider-track" not in shared_css


def test_map_colour_range_readouts_distinguish_minimum_and_maximum():
    from pasco_dash_app import map_color_readouts

    maximum, minimum, gradient = map_color_readouts([0.15, 0.85], "spectrum")

    assert maximum == "MAX  0.85 AU"
    assert minimum == "MIN  0.15 AU"
    assert "linear-gradient" in gradient["background"]
    assert "#6d00a8" in gradient["background"]

    css = (Path(__file__).parent / "assets" / "inflow.css").read_text()
    assert ".rc-slider-handle-1" in css
    assert ".rc-slider-handle-2" in css
    assert "background: #111111 !important" in css


def test_spectrum_palette_and_palette_menu_have_no_blue_interface_selection(tmp_path):
    from pasco_dash_app import isoabsorbance_figure

    path = tmp_path / "inline_00001_t0000.000s.csv"
    write_pasco_csv(path, 1, 0.5)
    figure = isoabsorbance_figure([path], color_scale="spectrum")
    scale = figure.data[0].colorscale

    assert scale[0][1] == "#6d00a8"
    assert scale[-1][1] == "#d00000"
    assert figure.data[0].showscale is False

    css = (Path(__file__).parent / "assets" / "inflow.css").read_text()
    assert ".inflow-map-palette .Select-option.is-selected" in css
    assert "background: #111111" in css
    assert ".inflow-map-palette .Select-option.is-focused" in css
    assert "background: #ececef" in css


def test_smooth_payload_carries_map_edge_columns_at_map_stride(tmp_path):
    from pasco_dash_app import smooth_animation_payload

    previous = tmp_path / "inline_00001_t0000.000s.csv"
    latest = tmp_path / "inline_00002_t0002.000s.csv"
    write_pasco_csv(previous, 1, 2.0)
    write_pasco_csv(latest, 2, 3.0)

    payload = smooth_animation_payload([previous, latest], 500.0)

    # Same stride the isoabsorbance map uses, so the engine can morph the
    # map's newest column exactly like the gliding A(t) tip.
    assert payload["map_prev"] == pytest.approx([2.0])
    assert payload["map_next"] == pytest.approx([3.0])


def test_map_holds_back_the_newest_column_for_the_engine_to_grow(tmp_path):
    from pasco_dash_app import isoabsorbance_figure

    names = ["inline_00001_t0000.000s.csv", "inline_00002_t0002.000s.csv", "inline_00003_t0004.000s.csv"]
    paths = []
    for i, name in enumerate(names, start=1):
        path = tmp_path / name
        write_pasco_csv(path, i, float(i))
        paths.append(path)

    held = isoabsorbance_figure(paths, 500.0, wavelength_stride=1, hold_last=True)

    # One-measurement display delay, like the A(t) tip: the newest spectrum's
    # column is absent and the browser engine grows it in continuously.
    assert list(held.data[0].x) == pytest.approx([0.0, 2.0])
    now_line = next(shape for shape in held.layout.shapes if shape.name == "map-now")
    assert now_line.x0 == pytest.approx(2.0)
    assert list(held.layout.xaxis.range) == pytest.approx([0.0, 2.0])


def test_absorbance_axes_never_scale_past_the_instrument_saturation_of_3():
    from pasco_dash_app import spectrum_y_range

    capped = spectrum_y_range([0.1, 2.9, 3.0, 3.0])
    assert capped[0] == 0.0  # baseline pinned, never jumps
    assert capped[1] == pytest.approx(3.0)

    modest = spectrum_y_range([0.1, 0.2])
    assert modest[1] < 0.3
