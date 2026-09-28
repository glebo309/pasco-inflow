#!/usr/bin/env python3
"""Framework-independent state and file helpers for the PASCO dashboard."""

from datetime import datetime
import json
from pathlib import Path
import shutil

from pasco_flow_data import elapsed_from_filename


def read_status(run_dir):
    path = Path(run_dir) / "status.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _display_run_name(name):
    try:
        return datetime.strptime(name, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return name


def run_choices(run_root):
    root = Path(run_root)
    if not root.exists():
        return []
    choices = []
    for run_dir in sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and (path / "status.json").is_file()
        ),
        reverse=True,
    ):
        status = read_status(run_dir)
        state = status.get("state", "saved")
        state_label = "recording" if state == "running" else state
        count = int(status.get("spectra_captured", 0) or 0)
        choices.append(
            {
                "label": f"{_display_run_name(run_dir.name)}  ·  {state_label}  ·  {count} spectra",
                "value": run_dir.name,
            }
        )
    return choices


def selected_run_value(names, selected, *, action=None, triggered_ids=()):
    """Resolve the visible run without auto-opening history from a chosen folder."""
    names = list(names)
    triggered_ids = set(triggered_ids or ())
    action = action or {}

    if "save-root-store" in triggered_ids and action.get("type") != "start":
        return None

    if action.get("type") == "start":
        started = action.get("run_name")
        if "action-store" in triggered_ids:
            return started if started in names else None
        if selected is None and started in names:
            return started

    return selected if selected in names else None


def latest_spectrum_path(run_dir):
    exports = Path(run_dir) / "exports"
    paths = list(exports.glob("inline_*_t*s.csv")) if exports.exists() else []
    if not paths:
        return None
    return max(paths, key=elapsed_from_filename)


def active_run(run_root):
    for choice in run_choices(run_root):
        run_dir = Path(run_root) / choice["value"]
        if read_status(run_dir).get("state") == "running":
            return run_dir
    return None


def remember_active_run(pointer_path, run_dir):
    pointer = Path(pointer_path)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    run_dir = Path(run_dir).resolve()
    pointer.write_text(json.dumps({"run_dir": str(run_dir)}))
    return run_dir


def discover_active_run(pointer_path, *, fallback_roots=()):
    pointer = Path(pointer_path)
    if pointer.exists():
        try:
            run_dir = Path(json.loads(pointer.read_text())["run_dir"]).resolve()
            if read_status(run_dir).get("state") == "running":
                return run_dir
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            pass
        pointer.unlink(missing_ok=True)

    for root in fallback_roots:
        running = active_run(root)
        if running is not None:
            return running.resolve()
    return None


def worker_command(
    *,
    python,
    worker,
    interval_s,
    duration_minutes,
    run_dir,
    scan_time_s=0.5,
    continuous=False,
    attach=False,
):
    command = [
        str(python),
        str(worker),
        "--interval",
        str(float(interval_s)),
        "--scan-time",
        str(float(scan_time_s)),
    ]
    if continuous:
        command.append("--continuous")
    else:
        command.extend(["--minutes", str(float(duration_minutes))])
    if attach:
        # Manual Record: the user pressed Record in PASCO themselves; the
        # worker only reads the live data and never clicks or switches apps.
        command.append("--attach")
    command.extend(["--run-dir", str(run_dir)])
    return command


def normalize_wavelength(value, *, minimum_nm=379.8, maximum_nm=950.0, default_nm=520.0):
    wavelength = default_nm if value is None else float(value)
    return min(max(wavelength, float(minimum_nm)), float(maximum_nm))


def wavelength_color(wavelength_nm):
    """Approximate sRGB hex of a wavelength; NIR above 780 nm fades toward dark."""
    nm = min(max(float(wavelength_nm), 380.0), 950.0)
    if nm <= 440:
        red, green, blue = (440 - nm) / 60.0, 0.0, 1.0
    elif nm <= 490:
        red, green, blue = 0.0, (nm - 440) / 50.0, 1.0
    elif nm <= 510:
        red, green, blue = 0.0, 1.0, (510 - nm) / 20.0
    elif nm <= 580:
        red, green, blue = (nm - 510) / 70.0, 1.0, 0.0
    elif nm <= 645:
        red, green, blue = 1.0, (645 - nm) / 65.0, 0.0
    else:
        red, green, blue = 1.0, 0.0, 0.0
    if nm > 780:
        fade = max(0.18, 1.0 - (nm - 780.0) / 200.0)
        red, green, blue = red * fade, green * fade, blue * fade
    return "#" + "".join(f"{round(255 * channel ** 0.8):02x}" for channel in (red, green, blue))


def wavelength_plot_color(wavelength_nm, *, ink=(21, 21, 26), blend=0.42):
    """Wavelength color pulled toward ink so plot traces stay legible on white."""
    vivid = wavelength_color(wavelength_nm)
    mixed = (
        round(int(vivid[position : position + 2], 16) * (1 - blend) + component * blend)
        for position, component in zip((1, 3, 5), ink)
    )
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


def resolve_save_root(value, default_root):
    """The stored save folder if it is still valid, otherwise the default root."""
    try:
        return validate_save_root(value)
    except ValueError:
        root = Path(default_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root


def display_path(path):
    text = str(Path(path))
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text


def validate_save_root(value):
    if not value:
        raise ValueError("Choose a save folder before starting the run")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Save location is not a folder: {root}")
    return root


def start_is_disabled(save_data, *, active_run_dir=None):
    """Only enable Start after the user confirms a valid folder for this run."""
    if active_run_dir is not None or not (save_data or {}).get("confirmed"):
        return True
    try:
        validate_save_root((save_data or {}).get("path"))
    except ValueError:
        return True
    return False


def clicked_wavelength(click_data):
    """Return the wavelength represented by a Plotly spectrum click."""
    try:
        wavelength_nm = float(click_data["points"][0]["x"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return normalize_wavelength(wavelength_nm)


# Measured 2026-08-20: one 2212-point export is ~82 KB; with its share of the
# consolidated matrix, manifest, and events the all-in cost is ~120 KB/spectrum.
BYTES_PER_SPECTRUM = 120_000


def format_bytes(size):
    size = float(size)
    if size >= 1e9:
        value = size / 1e9
        return f"{value:.1f} GB" if value < 10 else f"{value:.0f} GB"
    if size >= 1e6:
        return f"{size / 1e6:.0f} MB"
    return f"{max(size / 1e3, 1):.0f} KB"


def storage_estimate(interval_s, duration_minutes, *, continuous=False, bytes_per_spectrum=BYTES_PER_SPECTRUM):
    """Expected disk footprint of a run, from its schedule. None if unschedulable."""
    try:
        interval = 2.0 if interval_s is None else float(interval_s)
    except (TypeError, ValueError):
        return None
    if interval <= 0:
        return None
    if continuous:
        per_hour = bytes_per_spectrum * 3600.0 / interval
        return {
            "text": f"≈ {format_bytes(per_hour)} of data per hour, grows until stopped",
            "big": per_hour >= 1e9,
        }
    try:
        duration = 10.0 if duration_minutes is None else float(duration_minutes)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    spectra = max(int(round(duration * 60.0 / interval)), 1)
    total = spectra * bytes_per_spectrum
    big = total >= 1e9
    text = f"≈ {format_bytes(total)} of data ({spectra} spectra)"
    if big:
        text += ", this run will get large"
    return {"text": text, "big": big}


def validate_start_settings(*, interval_s, duration_minutes, continuous=False):
    interval = 2.0 if interval_s is None else float(interval_s)
    if interval < 1:
        raise ValueError("Spectrum interval must be at least 1 second")
    if continuous:
        return interval, None
    duration = 10.0 if duration_minutes is None else float(duration_minutes)
    if duration <= 0:
        raise ValueError("Run duration must be above 0")
    return interval, duration


def format_audit_log(events, *, limit=80):
    """Format acquisition events as a compact tail-style terminal log."""
    if not events:
        return "$ waiting for acquisition events…"

    lines = ["$ tail -f acquisition.log"]
    for event in list(events)[-int(limit):]:
        wall_time = str(event.get("wall_time", ""))
        timestamp = wall_time.rsplit("T", 1)[-1] if wall_time else "--:--:--.---"
        try:
            elapsed = f"+{float(event.get('elapsed_s', 0)):08.3f}s"
        except (TypeError, ValueError):
            elapsed = "+----.---s"
        phase = str(event.get("phase", "")).upper()[:7]
        try:
            spectrum = f"#{int(event.get('spectrum', 0)):05d}"
        except (TypeError, ValueError):
            spectrum = "#-----"
        detail = str(event.get("detail", "")).strip()
        filename = str(event.get("filename", "")).strip()
        message = detail
        if filename:
            message = f"{message}  →  {filename}" if message else filename
        lines.append(f"{timestamp}  {elapsed}  [{phase:<7}]  {spectrum}  {message}".rstrip())
    return "\n".join(lines)


def move_run_to_trash(run_root, run_name, *, trash_root=None):
    root = Path(run_root).resolve()
    source = (root / run_name).resolve()
    if source.parent != root or not source.is_dir():
        raise ValueError(f"Invalid run: {run_name}")
    if read_status(source).get("state") == "running":
        raise ValueError("Cannot delete a run while it is recording")

    trash = Path(trash_root) if trash_root is not None else Path.home() / ".Trash"
    trash.mkdir(parents=True, exist_ok=True)
    base = trash / f"PASCO_InFlow_{source.name}"
    destination = base
    counter = 2
    while destination.exists():
        destination = trash / f"{base.name}_{counter}"
        counter += 1
    return Path(shutil.move(str(source), str(destination)))
