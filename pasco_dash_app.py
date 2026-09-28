#!/usr/bin/env python3
"""Dash control and analysis interface for PASCO inline spectra."""

from datetime import datetime
from hashlib import sha1
from pathlib import Path
import csv
import json
import shutil
import subprocess
import sys
import traceback

from dash import ALL, ClientsideFunction, Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import MissingCallbackContextException, PreventUpdate
import pandas as pd
import plotly.graph_objects as go

from pasco_dashboard_logic import (
    active_run,
    clicked_wavelength,
    discover_active_run,
    display_path,
    format_audit_log,
    latest_spectrum_path,
    move_run_to_trash,
    normalize_wavelength,
    read_status,
    remember_active_run,
    resolve_save_root,
    run_choices,
    selected_run_value,
    start_is_disabled,
    storage_estimate,
    validate_save_root,
    validate_start_settings,
    wavelength_color,
    wavelength_plot_color,
    worker_command,
)
from pasco_flow_data import (
    absorbance_trace,
    consolidate_spectra,
    continuous_absorbance_trace,
    continuous_spectrum_frame,
    elapsed_from_filename,
    read_pasco_spectrum,
    spectrum_fade_opacities,
)
from pasco_internal_data import live_data_age_s
from pasco_ui import (
    choose_directory,
    choose_save_path,
    spectrometry_running,
    stop_recording_preserving_context,
)


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_RUN_ROOT = Path.home() / "Desktop" / "PASCO_InFlow"
ACTIVE_RUN_POINTER = Path.home() / "Library" / "Application Support" / "PASCO InFlow" / "active_run.json"
WORKER = PROJECT_DIR / "pasco_capture_worker.py"
PLOT_CONFIG = {
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
}
MAP_COLOR_SCALES = {
    "spectrum": [
        [0.0, "#6d00a8"],
        [0.16, "#3030d8"],
        [0.32, "#008fe8"],
        [0.48, "#00a651"],
        [0.64, "#e6d400"],
        [0.82, "#f06a00"],
        [1.0, "#d00000"],
    ],
    "ocean": [
        [0.0, "#f7fbff"],
        [0.18, "#dbeafe"],
        [0.45, "#60a5fa"],
        [0.72, "#2563eb"],
        [1.0, "#172554"],
    ],
    "inferno": [
        [0.0, "#000004"],
        [0.25, "#57106e"],
        [0.5, "#bc3754"],
        [0.75, "#f98e09"],
        [1.0, "#fcffa4"],
    ],
    "gray": [[0.0, "#ffffff"], [1.0, "#000000"]],
    "viridis": [
        [0.0, "#440154"],
        [0.25, "#3b528b"],
        [0.5, "#21918c"],
        [0.75, "#5ec962"],
        [1.0, "#fde725"],
    ],
}
MAP_COLOR_TICKS = ("3", "2", "1", "0")


def export_paths(run_dir):
    exports = Path(run_dir) / "exports"
    if not exports.exists():
        return []
    return sorted(exports.glob("inline_*_t*s.csv"), key=elapsed_from_filename)


def blank_figure(message, *, y_title="Absorbance (AU)"):
    figure = go.Figure()
    figure.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"size": 12, "color": "#6e6e73"},
    )
    figure.update_layout(
        template="plotly_white",
        autosize=True,
        xaxis={"visible": False},
        yaxis={"visible": False, "title": y_title},
        margin={"l": 54, "r": 16, "t": 12, "b": 42},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return figure


def standard_plot_layout(*, x_title, y_title, hovermode="closest"):
    return {
        "template": "plotly_white",
        "autosize": True,
        "margin": {"l": 58, "r": 16, "t": 12, "b": 46},
        "hovermode": hovermode,
        "hoverlabel": {"bgcolor": "white", "bordercolor": "rgba(18,18,24,0.30)", "font": {"size": 12}},
        "xaxis_title": x_title,
        "yaxis_title": y_title,
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif", "size": 13, "color": "#15151a"},
        "xaxis": {"gridcolor": "rgba(0,0,0,0.07)", "linecolor": "rgba(0,0,0,0.16)", "zeroline": False, "hoverformat": ".1f"},
        "yaxis": {"gridcolor": "rgba(0,0,0,0.07)", "linecolor": "rgba(0,0,0,0.16)", "zerolinecolor": "rgba(0,0,0,0.12)", "hoverformat": ".4f"},
    }


# The PS-2600A saturates at A = 3; never scale an absorbance axis past it.
ABSORBANCE_AXIS_CAP = 3.0


def spectrum_y_range(values):
    """Absorbance display window: the floor is pinned to exactly 0 so the
    baseline never jumps; only the top autoscales, capped at saturation."""
    finite = pd.Series(values).dropna()
    if finite.empty:
        return None
    high = float(finite.max())
    padding = max(high * 0.08, 0.002)
    top = min(high + padding, ABSORBANCE_AXIS_CAP)
    return [0.0, max(top, 0.01)]


def time_figure(paths, wavelength_nm, *, continuous=False, interval_s=2.0, now_epoch=None, line_color="#15151a"):
    trace = (
        continuous_absorbance_trace(
            paths,
            wavelength_nm,
            interval_s=interval_s,
            now_epoch=now_epoch,
        )
        if continuous
        else absorbance_trace(paths, wavelength_nm)
    )
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=trace["elapsed_s"],
            y=trace["absorbance_au"],
            mode="lines+markers",
            line={"color": line_color, "width": 2.2, "shape": "linear"},
            marker={"color": line_color, "size": 5},
            uid="time-line",
            hovertemplate="%{x:.1f} s<br>%{y:.4f} AU<extra></extra>",
            name=f"{wavelength_nm:.1f} nm" + (" · continuous" if continuous else " · raw"),
        )
    )
    if continuous and not trace.empty:
        figure.add_trace(
            go.Scatter(
                x=[trace["elapsed_s"].iloc[-1]],
                y=[trace["absorbance_au"].iloc[-1]],
                mode="markers",
                marker={"color": line_color, "size": 9, "line": {"color": "white", "width": 1.5}},
                uid="time-readhead",
                hoverinfo="skip",
                showlegend=False,
                name="Readhead",
            )
        )
    figure.update_layout(**standard_plot_layout(x_title="Elapsed time (s)", y_title="Absorbance (AU)"))
    figure.update_xaxes(minallowed=0)
    # Floor pinned at exactly 0, top autoscaling up to instrument saturation.
    figure.update_yaxes(rangemode="tozero", minallowed=0, maxallowed=ABSORBANCE_AXIS_CAP)
    return figure, trace


def spectrum_figure(paths, wavelength_nm, *, smooth=False, interval_s=2.0, now_epoch=None, marker_color="#f05a47"):
    figure = go.Figure()
    history = paths[-10:] if smooth else paths[-1:]

    def frame_identity(path):
        path = Path(path)
        run_name = path.parent.parent.name if path.parent.name == "exports" else path.parent.name
        return f"{run_name}:{path.name}"

    if smooth:
        # Static scaffold: this figure is sent to the browser ONCE per
        # run/wavelength change; assets/inflow_smooth.js then animates the
        # "spectrum-live" trace (and "spectrum-ghost" trail) between measured
        # frames at display refresh rate. Re-sending it per frame would fight
        # the client-side animation.
        displayed_spectrum = read_pasco_spectrum(history[-1])
        ghost = read_pasco_spectrum(history[-2]) if len(history) > 1 else displayed_spectrum
        figure.add_trace(
            go.Scatter(
                x=ghost.wavelength_nm,
                y=ghost.absorbance_au,
                mode="lines",
                line={"color": "#55555e", "width": 1.4},
                opacity=0.35,
                uid=f"spectrum-ghost-{frame_identity(ghost.path)}",
                hoverinfo="skip",
                showlegend=False,
                name="Previous",
            )
        )
        figure.add_trace(
            go.Scatter(
                x=displayed_spectrum.wavelength_nm,
                y=displayed_spectrum.absorbance_au,
                mode="lines",
                line={"color": "#15151a", "width": 2.6},
                opacity=1.0,
                # A new uid forces Plotly.react to mount the new measured
                # spectrum instead of preserving a stale trace in the browser.
                uid=f"spectrum-live-{frame_identity(displayed_spectrum.path)}",
                hovertemplate="%{x:.1f} nm<br>%{y:.4f} AU<extra></extra>",
                name="Live",
            )
        )
    else:
        for path in history[:-1]:
            spectrum = read_pasco_spectrum(path)
            figure.add_trace(
                go.Scatter(
                    x=spectrum.wavelength_nm,
                    y=spectrum.absorbance_au,
                    mode="lines",
                    line={"color": "rgba(21,21,26,0.10)", "width": 1},
                    hoverinfo="skip",
                    showlegend=False,
                )
            )
        displayed_spectrum = read_pasco_spectrum(history[-1])
    selected_absorbance = float(
        pd.Series(displayed_spectrum.absorbance_au, index=displayed_spectrum.wavelength_nm)
        .reindex([wavelength_nm], method="nearest")
        .iloc[0]
    )
    if not smooth:
        figure.add_trace(
            go.Scatter(
                x=displayed_spectrum.wavelength_nm,
                y=displayed_spectrum.absorbance_au,
                mode="lines",
                line={"color": "#15151a", "width": 2.2},
                uid=frame_identity(history[-1]),
                hovertemplate="%{x:.1f} nm<br>%{y:.4f} AU<extra></extra>",
                name=f"Latest · {displayed_spectrum.elapsed_s:.1f} s",
            )
        )
    figure.add_vline(x=wavelength_nm, line_dash="dot", line_color=marker_color, line_width=1.5)
    figure.add_annotation(
        x=wavelength_nm,
        y=selected_absorbance,
        text=f"{wavelength_nm:.1f} nm · {selected_absorbance:.3f} AU",
        showarrow=True,
        arrowhead=2,
        ax=58,
        ay=-42,
        bgcolor="white",
        bordercolor=marker_color,
        borderpad=5,
    )
    figure.update_layout(**standard_plot_layout(x_title="Wavelength (nm)", y_title="Absorbance (AU)"))
    y_range = spectrum_y_range(displayed_spectrum.absorbance_au)
    if y_range is not None:
        low, high = y_range
        figure.update_yaxes(range=y_range)
        # Invisible full-height columns so a click anywhere selects that wavelength
        # (Plotly clicks only register on trace points, never on empty plot area).
        step = 4
        wavelengths = pd.Series(displayed_spectrum.wavelength_nm).to_numpy(dtype=float)[::step]
        absorbances = pd.Series(displayed_spectrum.absorbance_au).to_numpy(dtype=float)[::step]
        spacing = (wavelengths[-1] - wavelengths[0]) / max(len(wavelengths) - 1, 1) if len(wavelengths) > 1 else 1.0
        figure.add_trace(
            go.Bar(
                x=wavelengths,
                y=[high - low] * len(wavelengths),
                base=low,
                width=spacing,
                marker={"color": "rgba(0,0,0,0.001)"},
                customdata=absorbances,
                hovertemplate="%{x:.1f} nm · %{customdata:.4f} AU<extra></extra>",
                showlegend=False,
                uid="wavelength-click-catcher",
                name="",
            )
        )
        figure.data = (figure.data[-1],) + figure.data[:-1]
    all_wavelengths = pd.Series(displayed_spectrum.wavelength_nm)
    if not all_wavelengths.empty:
        x_min = float(all_wavelengths.min())
        x_max = float(all_wavelengths.max())
        figure.update_xaxes(range=[x_min, x_max], minallowed=x_min, maxallowed=x_max)
    render_revision = frame_identity(displayed_spectrum.path)
    figure.update_layout(
        datarevision=render_revision,
        uirevision=render_revision,
        meta={"displayed_elapsed_s": displayed_spectrum.elapsed_s},
    )
    return figure


def isoabsorbance_figure(
    paths,
    wavelength_nm=None,
    *,
    marker_color="#f05a47",
    wavelength_stride=4,
    max_time_points=400,
    hold_last=False,
    color_range=(0.0, 0.8),
    color_scale="spectrum",
):
    """HPLC-DAD isoabsorbance map: time across, wavelength up, absorbance as color.

    With hold_last, the newest spectrum's column is left unpainted: the browser
    engine grows it in continuously (one-measurement display delay, exactly
    like the gliding A(t) tip), arriving as the next measurement lands.
    """
    if hold_last and len(paths) > 1:
        paths = paths[:-1]
    stride = max(1, -(-len(paths) // max_time_points))
    sampled = list(paths[::stride])
    if sampled[-1] != paths[-1]:
        sampled.append(paths[-1])
    spectra = [read_pasco_spectrum(path) for path in sampled]
    wavelengths = pd.Series(spectra[0].wavelength_nm).to_numpy(dtype=float)[::wavelength_stride]
    times = [float(spectrum.elapsed_s) for spectrum in spectra]
    columns = [
        pd.Series(spectrum.absorbance_au).to_numpy(dtype=float)[::wavelength_stride]
        for spectrum in spectra
    ]
    z_matrix = [list(row) for row in zip(*columns)]  # rows: wavelength, cols: time
    try:
        color_low, color_high = (float(color_range[0]), float(color_range[1]))
    except (IndexError, TypeError, ValueError):
        color_low, color_high = (0.0, 0.8)
    if color_high <= color_low:
        color_high = color_low + 0.05
    colorscale = MAP_COLOR_SCALES.get(color_scale, MAP_COLOR_SCALES["spectrum"])
    figure = go.Figure(
        go.Heatmap(
            x=times,
            y=wavelengths,
            z=z_matrix,
            zmin=color_low,
            zmax=color_high,
            colorscale=colorscale,
            showscale=False,
            zsmooth="best",
            hovertemplate="t=%{x:.1f} s<br>%{y:.1f} nm<br>%{z:.4f} AU<extra></extra>",
        )
    )
    if wavelength_nm is not None:
        figure.add_hline(y=wavelength_nm, line_dash="dot", line_color=marker_color, line_width=1.5)
    # Leading-edge marker; assets/inflow_smooth.js glides it (and the time
    # window) between measurements during continuous motion.
    figure.add_vline(x=times[-1], line_color="rgba(255,255,255,0.85)", line_width=1.5, name="map-now")
    figure.update_layout(**standard_plot_layout(x_title="Elapsed time (s)", y_title="Wavelength (nm)"))
    y_min = float(wavelengths.min())
    y_max = float(wavelengths.max())
    figure.update_xaxes(range=[0, times[-1]], minallowed=0)
    figure.update_yaxes(range=[y_min, y_max], minallowed=y_min, maxallowed=y_max)
    figure.update_layout(
        # Constant uirevision keeps the user's zoom while columns keep
        # appending during a run.
        uirevision="isoabsorbance",
        meta={"displayed_elapsed_s": times[-1]},
    )
    return figure


def smooth_animation_payload(paths, wavelength_nm):
    """Frames for the client-side animation engine: it interpolates the
    spectrum foreground, the A(t) line tip, and the readhead between the two
    newest measured spectra at display refresh rate."""
    if len(paths) < 2:
        return None
    previous = read_pasco_spectrum(paths[-2])
    latest = read_pasco_spectrum(paths[-1])
    trace = absorbance_trace(paths, wavelength_nm)
    if len(trace) < 2:
        return None

    def series(values):
        return [None if pd.isna(value) else float(value) for value in pd.Series(values)]

    elapsed = series(trace["elapsed_s"])
    absorbance = series(trace["absorbance_au"])
    interval = max((latest.elapsed_s or 0) - (previous.elapsed_s or 0), 0.2)
    # Leading-column colors for the isoabsorbance map, decimated with the same
    # stride the map itself uses so the engine can morph its newest column
    # from the previous spectrum into the latest one - the heatmap analog of
    # the gliding A(t) tip.
    map_stride = 4
    map_prev = series(pd.Series(previous.absorbance_au).to_numpy(dtype=float)[::map_stride])
    map_next = series(pd.Series(latest.absorbance_au).to_numpy(dtype=float)[::map_stride])
    return {
        "map_prev": map_prev,
        "map_next": map_next,
        # Full-resolution frames for the 2D spectrum: the scaffold shows the
        # previous spectrum and the engine morphs it into the latest one.
        "spec_prev": series(previous.absorbance_au),
        "spec_next": series(latest.absorbance_au),
        "spec_range_prev": spectrum_y_range(previous.absorbance_au),
        "spec_range_next": spectrum_y_range(latest.absorbance_au),
        "interval": float(interval),
        "line_x": elapsed[:-1],
        "line_y": absorbance[:-1],
        "head_prev": [elapsed[-2], absorbance[-2]],
        "head_next": [elapsed[-1], absorbance[-1]],
    }


def spectrum_frame_token(path):
    """Identity of one measured frame on disk (proven in pasco_minimal_live)."""
    path = Path(path).resolve()
    stat = path.stat()
    return sha1(f"{path}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()[:16]


def _panel_y_range(spectrum, *other_paths):
    """Y-range wide enough for this frame and any companion frames, so the
    glide morphs between them without any axis change."""
    low, high = spectrum_y_range(spectrum.absorbance_au) or [-0.002, 0.002]
    for other in other_paths:
        if other is None:
            continue
        try:
            other_range = spectrum_y_range(read_pasco_spectrum(other).absorbance_au)
        except Exception:
            other_range = None
        if other_range:
            low = min(low, other_range[0])
            high = max(high, other_range[1])
    return [low, high]


def minimal_spectrum_panel(path, prev_path=None, ghost_path=None, wavelength_nm=None, marker_color="#f05a47"):
    """The proven live spectrum from pasco_minimal_live, copied as-is: one
    scatter of the newest CSV, mounted as a brand-new component per frame.
    With prev_path, the y-range covers both frames so the glide engine can
    morph between them with restyles alone (no per-tick relayout)."""
    path = Path(path).resolve()
    spectrum = read_pasco_spectrum(path)
    token = spectrum_frame_token(path)
    wavelengths = pd.Series(spectrum.wavelength_nm)
    y_range = _panel_y_range(spectrum, prev_path, ghost_path)
    figure = go.Figure()
    # Invisible full-height columns so a click anywhere selects that
    # wavelength (Plotly clicks only register on trace points, never on
    # empty plot area).
    catcher_x = wavelengths.to_numpy(dtype=float)[::4]
    catcher_values = pd.Series(spectrum.absorbance_au).to_numpy(dtype=float)[::4]
    catcher_spacing = (
        (catcher_x[-1] - catcher_x[0]) / max(len(catcher_x) - 1, 1)
        if len(catcher_x) > 1
        else 1.0
    )
    figure.add_trace(
        go.Bar(
            x=catcher_x,
            y=[y_range[1] - y_range[0]] * len(catcher_x),
            base=y_range[0],
            width=catcher_spacing,
            marker={"color": "rgba(0,0,0,0)", "line": {"width": 0}},
            customdata=catcher_values,
            hovertemplate="%{x:.1f} nm · %{customdata:.4f} AU<extra></extra>",
            showlegend=False,
            uid="wavelength-click-catcher",
            name="",
        )
    )
    if ghost_path is not None:
        try:
            ghost = read_pasco_spectrum(ghost_path)
            figure.add_trace(
                go.Scatter(
                    x=ghost.wavelength_nm,
                    y=ghost.absorbance_au,
                    mode="lines",
                    line={"color": "#9a9aa2", "width": 1.6},
                    opacity=0.4,
                    uid="spectrum-ghost",
                    hoverinfo="skip",
                    showlegend=False,
                )
            )
        except Exception:
            pass  # the trail is decoration; never block the live curve
    figure.add_trace(
        go.Scatter(
            x=spectrum.wavelength_nm,
            y=spectrum.absorbance_au,
            mode="lines",
            line={"color": "#111111", "width": 2},
            uid="spectrum-live",  # hook for the 60 fps glide in inflow_smooth.js
            hovertemplate="%{x:.1f} nm<br>%{y:.5f} AU<extra></extra>",
        )
    )
    if wavelength_nm is not None:
        figure.add_vline(x=wavelength_nm, line_dash="dot", line_color=marker_color, line_width=1.5)
    figure.update_layout(
        template="plotly_white",
        margin={"l": 58, "r": 16, "t": 12, "b": 46},
        paper_bgcolor="white",
        plot_bgcolor="white",
        bargap=0,
        showlegend=False,
        autosize=True,
        datarevision=token,
        uirevision=token,
        xaxis={
            "title": "Wavelength (nm)",
            "range": [float(wavelengths.min()), float(wavelengths.max())],
            "showgrid": True,
            "gridcolor": "#eeeeee",
            "zeroline": False,
        },
        yaxis={
            "title": "Absorbance (AU)",
            "range": y_range,
            "minallowed": 0,
            "maxallowed": ABSORBANCE_AXIS_CAP,
            "showgrid": True,
            "gridcolor": "#eeeeee",
            "zeroline": True,
            "zerolinecolor": "#bbbbbb",
        },
    )
    graph = dcc.Graph(
        # Dict id: remount-per-frame identity AND pattern-matching clicks.
        id={"type": "spectrum-frame", "token": token},
        figure=figure,
        config={"displaylogo": False, "responsive": True},
        className="inflow-graph",
    )
    run_name = path.parent.parent.name
    status = f"{run_name} · {path.name} · t={spectrum.elapsed_s:.3f} s"
    return graph, status


def empty_spectrum_panel(message, token):
    return dcc.Graph(
        id=f"spectrum-empty-{token}",
        figure=blank_figure(message),
        config={"displayModeBar": False, "responsive": True},
        className="inflow-graph",
    )


def metric(label, value_id, initial="-"):
    return html.Div(
        className="inflow-metric",
        children=[html.Div(label, className="inflow-metric-label"), html.Div(initial, id=value_id, className="inflow-metric-value")],
    )


DEFAULT_RUN_ROOT.mkdir(parents=True, exist_ok=True)

app = Dash(__name__, title="PASCO InFlow", update_title=None)
server = app.server


@server.get("/health")
def health():
    # The folder identifies the running version so the launcher can tell a
    # leftover server from an older copy apart and replace it.
    return f"PASCO InFlow Dash @ {PROJECT_DIR}", 200, {"Content-Type": "text/plain"}


app.layout = html.Div(
    id="inflow-app",
    children=[
        dcc.Interval(id="poll", interval=500, n_intervals=0),
        dcc.Interval(id="device-poll", interval=4000, n_intervals=0),
        dcc.Store(id="action-store"),
        dcc.Store(id="pending-delete"),
        dcc.Store(id="save-root-store", storage_type="local"),
        # What this tab's figures currently show; lets polling skip unchanged
        # re-renders per browser tab instead of via shared server state.
        dcc.Store(id="render-signature"),
        # Latest two measured frames for the client-side smooth-motion engine.
        dcc.Store(id="smooth-frames"),
        html.Div(id="smooth-sink", style={"display": "none"}),
        dcc.ConfirmDialog(id="confirm-delete", message="Delete this run and all of its spectra? It can be recovered from the macOS Trash."),

        html.Header(
            className="inflow-header",
            children=[
                html.Div(
                    children=[
                        html.Div("PASCO InFlow", className="inflow-brand"),
                    ]
                ),
                html.Div(
                    className="inflow-header-status",
                    children=[
                        html.Div(id="run-status-badge", className="inflow-run-status inflow-status-idle", children="Idle"),
                        html.Div(id="device-badge", className="inflow-device inflow-device-checking", children="Checking PASCO…"),
                    ],
                ),
            ],
        ),
        html.Div(className="inflow-spectral-strip", title="Instrument range 380–950 nm"),

        html.Div(
            className="inflow-shell",
            children=[
                html.Aside(
                    className="inflow-sidebar",
                    children=[
                        html.Section(
                            className="inflow-side-section",
                            children=[
                                html.Div("Acquisition", className="inflow-side-title"),
                                html.Label("Spectrum interval", className="inflow-label"),
                                html.Div(
                                    className="inflow-input-unit",
                                    children=[
                                        dcc.Dropdown(
                                            id="interval-input",
                                            options=[
                                                {"label": "1", "value": 1},
                                                {"label": "2", "value": 2},
                                                {"label": "5", "value": 5},
                                                {"label": "10", "value": 10},
                                                {"label": "30", "value": 30},
                                                {"label": "60", "value": 60},
                                            ],
                                            value=2,
                                            clearable=False,
                                            searchable=False,
                                            className="inflow-setting-select",
                                        ),
                                        html.Span("seconds"),
                                    ],
                                ),
                                html.Label("Run duration", className="inflow-label"),
                                html.Div(
                                    className="inflow-input-unit",
                                    children=[
                                        dcc.Input(
                                            id="duration-input",
                                            type="number",
                                            value=10,
                                            min=0.1,
                                            step=0.1,
                                            className="inflow-number",
                                        ),
                                        html.Span("minutes", id="duration-unit"),
                                    ],
                                ),
                                dcc.Checklist(
                                    id="continuous-input",
                                    options=[{"label": "Run continuously until stopped", "value": "continuous"}],
                                    value=[],
                                    className="inflow-checklist",
                                    inputClassName="inflow-checkbox",
                                    labelClassName="inflow-check-label",
                                ),
                                html.Div(id="storage-estimate", className="inflow-storage"),
                                html.Div(
                                    className="inflow-save-head",
                                    children=[
                                        html.Label("Save location", className="inflow-label"),
                                        html.Button("Change…", id="choose-save-button", className="inflow-text-button", n_clicks=0),
                                    ],
                                ),
                                html.Div(id="save-location-display", className="inflow-save-location"),
                                html.Div(
                                    "Press Record in PASCO first - Start run then reads its live data.",
                                    className="inflow-hint",
                                ),
                                html.Div(
                                    className="inflow-run-buttons",
                                    children=[
                                        html.Button("Start run", id="start-button", className="inflow-button inflow-button-primary", n_clicks=0, disabled=True),
                                        html.Button("Stop", id="stop-button", className="inflow-button inflow-button-secondary", n_clicks=0, disabled=True),
                                    ],
                                ),
                                html.Div(id="control-message", className="inflow-control-message"),
                            ],
                        ),
                        html.Section(
                            className="inflow-side-section",
                            children=[
                                html.Div("Saved runs", className="inflow-side-title"),
                                dcc.Dropdown(
                                    id="selected-run",
                                    options=[],
                                    value=None,
                                    clearable=False,
                                    searchable=False,
                                    placeholder="Select a saved run…",
                                    className="inflow-run-select",
                                ),
                                html.Button("Delete run", id="delete-button", className="inflow-button inflow-button-danger", n_clicks=0, disabled=True),
                            ],
                        ),
                        # ASCII flow cell, animated by assets/inflow_flowart.js
                        # while a run is recording.
                        html.Pre(id="flow-art", className="inflow-art"),
                    ],
                ),

                html.Main(
                    className="inflow-main",
                    children=[
                        html.Div(
                            className="inflow-instrument-strip",
                            children=[
                                metric("Spectra", "metric-spectra"),
                                metric("Elapsed", "metric-elapsed"),
                                metric("Interval", "metric-interval"),
                                html.Div(className="inflow-strip-divider"),
                                html.Div(
                                    className="inflow-wavelength-control",
                                    children=[
                                        html.Div("Analysis wavelength", className="inflow-metric-label"),
                                        html.Div(
                                            className="inflow-wavelength-entry",
                                            children=[
                                                html.Span(id="wavelength-swatch", className="inflow-wavelength-swatch"),
                                                dcc.Input(
                                                    id="wavelength-input",
                                                    type="number",
                                                    min=380,
                                                    max=950,
                                                    step=0.5,
                                                    value=520,
                                                    debounce=False,
                                                    className="inflow-wavelength-number",
                                                ),
                                                html.Span("nm", className="inflow-wavelength-unit"),
                                            ],
                                        ),
                                    ],
                                ),
                                html.Div(className="inflow-strip-divider"),
                                html.Div(
                                    className="inflow-display-control",
                                    children=[
                                        html.Div("Display", className="inflow-metric-label"),
                                        html.Div(
                                            className="inflow-motion-row",
                                            children=[
                                                dcc.Checklist(
                                                    id="smooth-display",
                                                    options=[{"label": "Continuous motion", "value": "smooth"}],
                                                    value=["smooth"],
                                                    className="inflow-checklist inflow-smooth-toggle",
                                                    inputClassName="inflow-checkbox",
                                                    labelClassName="inflow-check-label",
                                                ),
                                                html.Span(
                                                    "?",
                                                    className="inflow-info-badge",
                                                    title="The time readhead and full-spectrum foreground move between measured frames during the following interval. Previous raw spectra fade behind them. One-sample visual delay; exports remain raw.",
                                                ),
                                                dcc.RadioItems(
                                                    id="spectrum-view",
                                                    options=[
                                                        {"label": "2D", "value": "2d"},
                                                        {"label": "3D", "value": "3d"},
                                                    ],
                                                    value="2d",
                                                    className="inflow-view-toggle",
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        html.Div(
                            id="plot-grid",
                            className="inflow-grid",
                            children=[
                                html.Section(
                                    className="inflow-card",
                                    children=[
                                        html.Div(
                                            className="inflow-card-header",
                                            children=[html.H2(id="time-title", children="Absorbance over time"), html.Button("Export trace…", id="download-trace-button", className="inflow-link-button", disabled=True)],
                                        ),
                                        dcc.Graph(
                                            id="time-graph",
                                            figure=blank_figure("Start a run or select saved data"),
                                            config=PLOT_CONFIG,
                                            animate=False,
                                            className="inflow-graph",
                                        ),
                                    ],
                                ),
                                html.Section(
                                    className="inflow-card",
                                    children=[
                                        html.Div(
                                            className="inflow-card-header",
                                            children=[
                                                html.H2(id="spectrum-title", children="Full spectrum"),
                                                html.Div(
                                                    className="inflow-card-actions",
                                                    children=[
                                                        html.Button("Export spectrum…", id="download-spectrum-button", className="inflow-link-button", disabled=True),
                                                        html.Button("Export matrix…", id="download-matrix-button", className="inflow-link-button", disabled=True),
                                                    ],
                                                ),
                                            ],
                                        ),
                                        html.Div(
                                            id="spectrum-slot",
                                            className="inflow-graph",
                                        ),
                                    ],
                                ),
                                html.Section(
                                    className="inflow-card inflow-card-surface",
                                    children=[
                                        html.Div(
                                            className="inflow-card-header",
                                            children=[html.H2(id="surface-title", children="Isoabsorbance map")],
                                        ),
                                        html.Div(
                                            className="inflow-map-body",
                                            children=[
                                                dcc.Graph(
                                                    id="surface-graph",
                                                    figure=blank_figure("Record spectra to build the map"),
                                                    config=PLOT_CONFIG,
                                                    animate=False,
                                                    className="inflow-graph",
                                                ),
                                                html.Aside(
                                                    className="inflow-map-controls",
                                                    children=[
                                                        html.Div("Colour range", className="inflow-map-control-label"),
                                                        html.Div("A (AU)", className="inflow-map-axis-title"),
                                                        html.Div(
                                                            id="map-color-max",
                                                            className="inflow-map-bound inflow-map-bound-max",
                                                            children="MAX  0.80 AU",
                                                        ),
                                                        html.Div(
                                                            className="inflow-map-range",
                                                            children=[
                                                                html.Div(
                                                                    id="map-color-gradient",
                                                                    className="inflow-map-gradient",
                                                                    style={
                                                                        "background": "linear-gradient(to top, #000004 0%, #57106e 25%, #bc3754 50%, #f98e09 75%, #fcffa4 100%)"
                                                                    },
                                                                ),
                                                                dcc.RangeSlider(
                                                                    id="map-color-range",
                                                                    min=0.0,
                                                                    max=3.0,
                                                                    step=0.05,
                                                                    value=[0.0, 0.8],
                                                                    marks={value: str(value) for value in range(4)},
                                                                    vertical=True,
                                                                    verticalHeight=500,
                                                                    tooltip={"placement": "left", "always_visible": False},
                                                                ),
                                                                html.Div(
                                                                    id="map-color-ticks",
                                                                    className="inflow-map-ticks",
                                                                    children=[html.Span(value) for value in MAP_COLOR_TICKS],
                                                                ),
                                                            ],
                                                        ),
                                                        html.Div(
                                                            id="map-color-min",
                                                            className="inflow-map-bound inflow-map-bound-min",
                                                            children="MIN  0.00 AU",
                                                        ),
                                                        html.Div("Palette", className="inflow-map-control-label"),
                                                        dcc.Dropdown(
                                                            id="map-color-scale",
                                                            options=[
                                                                {"label": "Spectrum", "value": "spectrum"},
                                                                {"label": "Ocean", "value": "ocean"},
                                                                {"label": "Inferno", "value": "inferno"},
                                                                {"label": "Gray", "value": "gray"},
                                                                {"label": "Viridis", "value": "viridis"},
                                                            ],
                                                            value="spectrum",
                                                            clearable=False,
                                                            searchable=False,
                                                            className="inflow-map-palette",
                                                        ),
                                                    ],
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        html.Div(id="export-message", className="inflow-export-message"),
                        html.Details(
                            className="inflow-details",
                            open=True,
                            children=[
                                html.Summary("Acquisition audit"),
                                html.Div(
                                    className="inflow-details-body",
                                    children=[
                                        html.Pre(
                                            id="audit-terminal",
                                            className="inflow-terminal",
                                            children="$ waiting for acquisition events…",
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        ),
    ],
)


@app.callback(
    Output("device-badge", "children"),
    Output("device-badge", "className"),
    Input("device-poll", "n_intervals"),
)
def update_device_status(_):
    if not spectrometry_running():
        return "Open PASCO Spectrometry", "inflow-device inflow-device-error"
    # Filesystem freshness of PASCO's live arrays: needs no permissions and
    # works on every Mac, unlike accessibility probes.
    age = live_data_age_s()
    if age is not None and age < 5.0:
        return "PASCO recording", "inflow-device inflow-device-ready"
    return "Press Record in PASCO", "inflow-device inflow-device-attn"


@app.callback(
    Output("duration-input", "disabled"),
    Output("duration-unit", "children"),
    Input("continuous-input", "value"),
)
def update_duration_mode(continuous_values):
    continuous = "continuous" in (continuous_values or [])
    return continuous, "until stopped" if continuous else "minutes"


@app.callback(
    Output("storage-estimate", "children"),
    Output("storage-estimate", "className"),
    Input("interval-input", "value"),
    Input("duration-input", "value"),
    Input("continuous-input", "value"),
)
def update_storage_estimate(interval_s, duration_minutes, continuous_values):
    estimate = storage_estimate(
        interval_s,
        duration_minutes,
        continuous="continuous" in (continuous_values or []),
    )
    if estimate is None:
        return "", "inflow-storage"
    if estimate["big"]:
        return estimate["text"], "inflow-storage inflow-storage-warn"
    return estimate["text"], "inflow-storage"


@app.callback(
    Output("plot-grid", "className"),
    Input("spectrum-view", "value"),
)
def toggle_plot_grid(view):
    return "inflow-grid inflow-grid-3d" if view == "3d" else "inflow-grid"


@app.callback(
    Output("wavelength-input", "value", allow_duplicate=True),
    Input("surface-graph", "clickData"),
    prevent_initial_call=True,
)
def select_wavelength_from_map(click_data):
    try:
        wavelength_nm = float(click_data["points"][0]["y"])
    except (KeyError, IndexError, TypeError, ValueError):
        return no_update
    return round(normalize_wavelength(wavelength_nm) * 2) / 2


@app.callback(
    Output("save-location-display", "children"),
    Output("save-location-display", "title"),
    Input("save-root-store", "data"),
)
def show_save_location(save_data):
    root = resolve_save_root((save_data or {}).get("path"), DEFAULT_RUN_ROOT)
    return display_path(root), str(root)


@app.callback(
    Output("wavelength-swatch", "style"),
    Input("wavelength-input", "value"),
)
def update_wavelength_swatch(value):
    return {"background": wavelength_color(normalize_wavelength(value))}


@app.callback(
    Output("map-color-max", "children"),
    Output("map-color-min", "children"),
    Output("map-color-gradient", "style"),
    Input("map-color-range", "value"),
    Input("map-color-scale", "value"),
)
def map_color_readouts(values, color_scale="spectrum"):
    try:
        minimum, maximum = float(values[0]), float(values[1])
    except (IndexError, TypeError, ValueError):
        minimum, maximum = 0.0, 0.8
    scale = MAP_COLOR_SCALES.get(color_scale, MAP_COLOR_SCALES["spectrum"])
    stops = ", ".join(f"{color} {position * 100:g}%" for position, color in scale)
    gradient = {"background": f"linear-gradient(to top, {stops})"}
    return f"MAX  {maximum:.2f} AU", f"MIN  {minimum:.2f} AU", gradient


# Click-to-select wavelength on the spectrum panel is handled entirely in the
# browser (assets/inflow_clicks.js): the panel remounts per measured frame, so
# Plotly clickData could die with the old component and clicks were lost.


@app.callback(
    Output("confirm-delete", "displayed"),
    Output("pending-delete", "data"),
    Input("delete-button", "n_clicks"),
    State("selected-run", "value"),
    prevent_initial_call=True,
)
def request_delete(_clicks, run_name):
    if not run_name:
        raise PreventUpdate
    return True, run_name


@app.callback(
    Output("action-store", "data"),
    Output("control-message", "children"),
    Output("save-root-store", "data"),
    Input("choose-save-button", "n_clicks"),
    Input("start-button", "n_clicks"),
    Input("stop-button", "n_clicks"),
    Input("confirm-delete", "submit_n_clicks"),
    State("pending-delete", "data"),
    State("selected-run", "value"),
    State("interval-input", "value"),
    State("duration-input", "value"),
    State("continuous-input", "value"),
    State("save-root-store", "data"),
    prevent_initial_call=True,
)
def acquisition_action(
    _choose,
    _start,
    _stop,
    _delete,
    pending_delete,
    selected_run,
    interval_s,
    duration_minutes,
    continuous_values,
    save_data,
):
    trigger = ctx.triggered_id
    current_path = (save_data or {}).get("path")

    if trigger == "choose-save-button":
        if discover_active_run(ACTIVE_RUN_POINTER, fallback_roots=[DEFAULT_RUN_ROOT]):
            return no_update, "Stop the active run before changing its save folder.", no_update
        selected_root = choose_directory(current_path or DEFAULT_RUN_ROOT)
        if selected_root is None:
            return no_update, "Save-folder selection cancelled.", no_update
        return (
            no_update,
            f"New runs will be saved under {display_path(selected_root)}.",
            {"path": str(selected_root)},
        )

    if trigger == "start-button":
        run_root = resolve_save_root(current_path, DEFAULT_RUN_ROOT)
        if discover_active_run(ACTIVE_RUN_POINTER, fallback_roots=[DEFAULT_RUN_ROOT]):
            return no_update, "A run is already recording. Stop it before starting another.", no_update
        continuous = "continuous" in (continuous_values or [])
        try:
            interval_s, duration_minutes = validate_start_settings(
                interval_s=interval_s,
                duration_minutes=duration_minutes,
                continuous=continuous,
            )
        except (TypeError, ValueError) as exc:
            return no_update, str(exc), no_update

        run_name = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = run_root / run_name
        suffix = 2
        while run_dir.exists():
            run_dir = run_root / f"{run_name}_{suffix}"
            suffix += 1
        run_dir.mkdir(parents=True)
        log_handle = (run_dir / "worker.log").open("w")
        command = worker_command(
            python=sys.executable,
            worker=WORKER,
            interval_s=interval_s,
            duration_minutes=duration_minutes,
            run_dir=run_dir,
            continuous=continuous,
            # Always attach: the user presses Record in PASCO themselves and
            # the run only reads live data. The automated clicking/window
            # switching proved unreliable across Macs and is retired.
            attach=True,
        )
        subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        remember_active_run(ACTIVE_RUN_POINTER, run_dir)
        message = (
            "Run started, reading PASCO's live data. PASCO's red Record button must already be on."
            + (" Press Stop run when finished." if continuous else "")
        )
        return (
            {"type": "start", "run_name": run_dir.name, "at": datetime.now().isoformat()},
            message,
            {"path": str(run_root)},
        )

    if trigger == "stop-button":
        run_dir = discover_active_run(ACTIVE_RUN_POINTER, fallback_roots=[DEFAULT_RUN_ROOT])
        if run_dir is None:
            return no_update, "There is no active run to stop.", no_update
        (run_dir / "STOP").touch()
        return (
            {"type": "stop", "run_name": run_dir.name, "at": datetime.now().isoformat()},
            "Stopping…",
            no_update,
        )

    if trigger == "confirm-delete":
        if not pending_delete:
            raise PreventUpdate
        try:
            run_root = resolve_save_root(current_path, DEFAULT_RUN_ROOT)
            move_run_to_trash(run_root, pending_delete)
        except ValueError as exc:
            return no_update, str(exc), no_update
        return {"type": "delete", "run_name": pending_delete, "at": datetime.now().isoformat()}, "Run deleted. It can be recovered from the Trash.", no_update

    raise PreventUpdate


@app.callback(
    Output("selected-run", "options"),
    Output("selected-run", "value"),
    Input("poll", "n_intervals"),
    Input("action-store", "data"),
    Input("save-root-store", "data"),
    State("selected-run", "value"),
)
def refresh_run_choices(_poll, action, save_data, selected):
    run_root = resolve_save_root((save_data or {}).get("path"), DEFAULT_RUN_ROOT)
    choices = run_choices(run_root)
    names = [choice["value"] for choice in choices]
    try:
        triggered = {entry["prop_id"].split(".")[0] for entry in ctx.triggered}
    except MissingCallbackContextException:
        triggered = set()
    return choices, selected_run_value(
        names,
        selected,
        action=action,
        triggered_ids=triggered,
    )


@app.callback(
    Output("run-status-badge", "children"),
    Output("run-status-badge", "className"),
    Output("metric-spectra", "children"),
    Output("metric-elapsed", "children"),
    Output("metric-interval", "children"),
    Output("time-title", "children"),
    Output("spectrum-title", "children"),
    Output("surface-title", "children"),
    Output("time-graph", "figure"),
    Output("spectrum-slot", "children"),
    Output("surface-graph", "figure"),
    Output("audit-terminal", "children"),
    Output("download-spectrum-button", "disabled"),
    Output("download-trace-button", "disabled"),
    Output("download-matrix-button", "disabled"),
    Output("start-button", "disabled"),
    Output("stop-button", "disabled"),
    Output("delete-button", "disabled"),
    Output("choose-save-button", "disabled"),
    Output("render-signature", "data"),
    Output("smooth-frames", "data"),
    Input("poll", "n_intervals"),
    Input("selected-run", "value"),
    Input("wavelength-input", "value"),
    Input("smooth-display", "value"),
    Input("spectrum-view", "value"),
    Input("save-root-store", "data"),
    Input("map-color-range", "value"),
    Input("map-color-scale", "value"),
    State("render-signature", "data"),
)
def refresh_dashboard(
    _poll,
    run_name,
    wavelength_nm,
    smooth_values,
    view,
    save_data,
    color_range=None,
    color_scale="spectrum",
    client_signature=None,
):
    # Preserve the direct-call API used by diagnostics written before the map
    # colour controls became callback inputs.
    if client_signature is None and isinstance(color_range, dict):
        client_signature = color_range
        color_range = None
        color_scale = "spectrum"
    color_range = color_range or [0.0, 0.8]
    color_scale = color_scale if color_scale in MAP_COLOR_SCALES else "spectrum"
    run_root = resolve_save_root((save_data or {}).get("path"), DEFAULT_RUN_ROOT)
    active = discover_active_run(ACTIVE_RUN_POINTER, fallback_roots=[DEFAULT_RUN_ROOT])
    if active is not None:
        run_root = active.parent
    disable_start = active is not None
    smooth = "smooth" in (smooth_values or [])
    client = client_signature if isinstance(client_signature, dict) else {}
    if not run_name:
        time_idle = client.get("time") == ["idle"]
        spectrum_idle = client.get("spectrum") == ["idle"]
        surface_idle = client.get("surface") == ["idle"]
        empty_time = no_update if time_idle else blank_figure("Start a run to begin measuring")
        empty_spectrum = (
            no_update
            if spectrum_idle
            else empty_spectrum_panel("The latest calibrated spectrum will appear here", "idle")
        )
        empty_surface = (
            no_update if surface_idle else blank_figure("Record spectra to build the map")
        )
        all_idle = time_idle and spectrum_idle and surface_idle
        new_signature = (
            no_update
            if all_idle
            else {"time": ["idle"], "spectrum": ["idle"], "surface": ["idle"], "frames": None}
        )
        animation_frames = no_update if all_idle else None
        return "Idle", "inflow-run-status inflow-status-idle", "0", "-", "-", "Absorbance over time", "Full spectrum", "Isoabsorbance map", empty_time, empty_spectrum, empty_surface, "$ waiting for acquisition events…", True, True, True, disable_start, active is None, True, active is not None, new_signature, animation_frames

    if run_root is None:
        raise PreventUpdate
    run_dir = run_root / run_name
    status = read_status(run_dir)
    state = status.get("state", "saved")
    state_label = "Recording" if state == "running" else state.capitalize()
    status_class = f"inflow-run-status inflow-status-{state}"
    captured = int(status.get("spectra_captured", 0) or 0)
    planned = status.get("spectra_planned")
    spectra_text = f"{captured} / {planned}" if planned else str(captured)
    elapsed = status.get("elapsed_s")
    elapsed_text = f"{float(elapsed):.1f} s" if elapsed is not None else "-"
    interval = status.get("interval_s")
    interval_text = f"{float(interval):g} s" if interval else "-"
    paths = export_paths(run_dir)
    wavelength_nm = normalize_wavelength(wavelength_nm)

    if paths:
        try:
            first = read_pasco_spectrum(paths[0])
            wavelength_nm = normalize_wavelength(
                wavelength_nm,
                minimum_nm=float(first.wavelength_nm.min()),
                maximum_nm=float(first.wavelength_nm.max()),
            )
            # In smooth mode the figure is a static scaffold: send it only when
            # the run, wavelength, or mode changes, and ship one animation
            # payload per new measured spectrum instead. In raw mode the figure
            # itself updates once per new spectrum.
            three_d = view == "3d"
            time_signature = (
                [run_name, "smooth-time", paths[-1].name, round(wavelength_nm, 3)]
                if smooth
                else [run_name, paths[-1].name, len(paths), round(wavelength_nm, 3)]
            )
            surface_signature = [
                run_name,
                "isoplot",
                paths[-1].name,
                len(paths),
                round(wavelength_nm, 3),
                smooth,
                [round(float(value), 3) for value in color_range],
                color_scale,
            ]
            frames_signature = [run_name, paths[-1].name, round(wavelength_nm, 3)] if smooth else None
            # The 3D surface replaces both 2D charts; hidden graphs are not
            # rendered, and their stored signatures are dropped so switching
            # views re-renders them fresh.
            send_time = not three_d and client.get("time") != time_signature
            send_surface = three_d and client.get("surface") != surface_signature
            send_frames = smooth and client.get("frames") != frames_signature
            clear_frames = client.get("frames") is not None and not smooth
            displayed_elapsed = read_pasco_spectrum(paths[-1]).elapsed_s
            accent = wavelength_plot_color(wavelength_nm)
            if send_time:
                time_plot, _trace = time_figure(
                    paths,
                    wavelength_nm,
                    continuous=smooth,
                    interval_s=float(interval or 2.0),
                    line_color=accent,
                )
            else:
                time_plot = no_update
            # Spectrum: the pasco_minimal_live panel, remounted once per
            # measured file - same contract as A(t) and the map. Continuous
            # mode mounts the PREVIOUS spectrum and the engine glides it into
            # the newest at 60 fps, landing exactly as the next file arrives.
            # Never re-send between measurements: that snaps the glide back.
            spectrum_token = spectrum_frame_token(paths[-1])
            spectrum_key = ["frame", spectrum_token, smooth, round(wavelength_nm, 1)]
            if three_d:
                send_slot = False
                spectrum_slot = no_update
                spectrum_status = None
            elif client.get("spectrum") == spectrum_key:
                send_slot = False
                spectrum_slot = no_update
                spectrum_status = None
            elif smooth and len(paths) > 1:
                send_slot = True
                # The ghost mounts as a copy of the live frame; the engine
                # fades it out over exactly one sample interval while the live
                # curve glides away, leaving a trail in sync with sampling.
                spectrum_slot, spectrum_status = minimal_spectrum_panel(
                    paths[-2],
                    prev_path=paths[-1],
                    ghost_path=paths[-2],
                    wavelength_nm=wavelength_nm,
                    marker_color=accent,
                )
            else:
                send_slot = True
                spectrum_slot, spectrum_status = minimal_spectrum_panel(
                    paths[-1],
                    wavelength_nm=wavelength_nm,
                    marker_color=accent,
                )
            surface_plot = (
                isoabsorbance_figure(
                    paths,
                    wavelength_nm,
                    marker_color=accent,
                    hold_last=smooth,
                    color_range=color_range,
                    color_scale=color_scale,
                )
                if send_surface
                else no_update
            )
            if send_frames:
                animation_frames = smooth_animation_payload(paths, wavelength_nm)
            elif clear_frames:
                animation_frames = None
            else:
                animation_frames = no_update
            if send_time or send_surface or send_frames or clear_frames or send_slot:
                new_signature = dict(client)
                if send_time:
                    new_signature["time"] = time_signature
                if send_surface:
                    new_signature["surface"] = surface_signature
                if send_slot:
                    new_signature["spectrum"] = spectrum_key
                if three_d:
                    new_signature.pop("time", None)
                    new_signature.pop("spectrum", None)
                else:
                    new_signature.pop("surface", None)
                new_signature["frames"] = frames_signature if send_frames else (
                    None if clear_frames else client.get("frames")
                )
            else:
                new_signature = no_update
            mode_label = " · continuous (1 sample delay)" if smooth else " · raw"
            time_title = f"Absorbance at {wavelength_nm:.1f} nm{mode_label}"
            spectrum_title = "Full spectrum"
            surface_title = (
                f"Isoabsorbance map · {len(paths)} spectra · t={displayed_elapsed:.1f} s"
                if three_d
                else no_update
            )
        except Exception:
            # A spectrum file can be mid-write during recording; keep the
            # current display for this frame and retry on the next poll
            # instead of failing the whole update.
            print(f"[inflow] skipped frame for {run_name}:", file=sys.stderr)
            traceback.print_exc()
            time_plot = no_update
            spectrum_slot = no_update
            surface_plot = no_update
            time_title = no_update
            spectrum_title = no_update
            surface_title = no_update
            new_signature = no_update
            animation_frames = no_update
    else:
        phase = status.get("phase", "waiting").replace("_", " ").capitalize()
        time_plot = blank_figure(f"{phase}: waiting for the first spectrum")
        spectrum_slot = empty_spectrum_panel("Waiting for PASCO calibrated data", f"waiting-{run_name}")
        surface_plot = blank_figure("Record spectra to build the map")
        time_title = f"Absorbance at {wavelength_nm:.1f} nm" + (" · continuous" if smooth else " · raw")
        spectrum_title = "Full spectrum"
        surface_title = "Isoabsorbance map"
        new_signature = {"time": ["waiting", run_name], "spectrum": ["waiting", run_name]}
        animation_frames = None

    events_path = run_dir / "events.csv"
    events = []
    if events_path.exists():
        try:
            with events_path.open(newline="") as handle:
                events = list(csv.DictReader(handle))
        except OSError:
            events = []

    is_running = state == "running"
    return (
        state_label,
        status_class,
        spectra_text,
        elapsed_text,
        interval_text,
        time_title,
        spectrum_title,
        surface_title,
        time_plot,
        spectrum_slot,
        surface_plot,
        format_audit_log(events, limit=6),
        not bool(paths),
        not bool(paths),
        not bool(paths),
        disable_start,
        active is None,
        is_running,
        active is not None,
        new_signature,
        animation_frames,
    )


@app.callback(
    Output("export-message", "children"),
    Input("download-spectrum-button", "n_clicks"),
    Input("download-trace-button", "n_clicks"),
    Input("download-matrix-button", "n_clicks"),
    State("selected-run", "value"),
    State("save-root-store", "data"),
    State("wavelength-input", "value"),
    prevent_initial_call=True,
)
def export_data(_spectrum, _trace, _matrix, run_name, save_data, wavelength_nm):
    if not run_name:
        raise PreventUpdate
    try:
        run_root = validate_save_root((save_data or {}).get("path"))
    except ValueError as exc:
        return str(exc)
    run_dir = run_root / run_name
    paths = export_paths(run_dir)
    if not paths:
        return "No spectra are available to export."

    trigger = ctx.triggered_id
    if trigger == "download-spectrum-button":
        source = latest_spectrum_path(run_dir)
        suggested_name = f"{run_name}_latest_spectrum.csv"
        destination = choose_save_path(suggested_name, initial_dir=run_root)
        if destination is None:
            return "Spectrum export cancelled."
        shutil.copy2(source, destination)
    elif trigger == "download-trace-button":
        wavelength_nm = normalize_wavelength(wavelength_nm)
        suggested_name = f"{run_name}_A_{wavelength_nm:.1f}nm_over_time.csv"
        destination = choose_save_path(suggested_name, initial_dir=run_root)
        if destination is None:
            return "Trace export cancelled."
        absorbance_trace(paths, wavelength_nm).to_csv(destination, index=False)
    elif trigger == "download-matrix-button":
        suggested_name = f"{run_name}_full_spectral_matrix.csv"
        destination = choose_save_path(suggested_name, initial_dir=run_root)
        if destination is None:
            return "Matrix export cancelled."
        matrix_path = run_dir / "consolidated_spectra.csv"
        if not matrix_path.exists():
            consolidate_spectra(paths).to_csv(matrix_path, index=False)
        shutil.copy2(matrix_path, destination)
    else:
        raise PreventUpdate

    return f"Saved · {destination}"


app.clientside_callback(
    ClientsideFunction(namespace="inflow", function_name="register_frames"),
    Output("smooth-sink", "children"),
    Input("smooth-frames", "data"),
)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=9124, debug=False)
