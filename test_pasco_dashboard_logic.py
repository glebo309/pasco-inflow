import json
from pathlib import Path

import pytest

from pasco_dashboard_logic import (
    active_run,
    clicked_wavelength,
    discover_active_run,
    display_path,
    format_audit_log,
    latest_spectrum_path,
    move_run_to_trash,
    normalize_wavelength,
    resolve_save_root,
    run_choices,
    selected_run_value,
    start_is_disabled,
    remember_active_run,
    validate_save_root,
    validate_start_settings,
    wavelength_color,
    wavelength_plot_color,
    worker_command,
)


def write_status(run_dir: Path, state: str, **extra) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"state": state, **extra}))


def test_run_choices_are_newest_first_and_include_capture_counts(tmp_path):
    older = tmp_path / "20260820_120000"
    newer = tmp_path / "20260820_130000"
    write_status(older, "complete", spectra_captured=12)
    write_status(newer, "running", spectra_captured=3)

    choices = run_choices(tmp_path)

    assert [choice["value"] for choice in choices] == [newer.name, older.name]
    assert choices[0]["label"] == "2026-08-20 13:00:00  ·  recording  ·  3 spectra"
    assert choices[1]["label"] == "2026-08-20 12:00:00  ·  complete  ·  12 spectra"


def test_run_choices_ignore_unrelated_folders_in_a_chosen_location(tmp_path):
    write_status(tmp_path / "20260820_130000", "complete", spectra_captured=2)
    (tmp_path / "unrelated-project").mkdir()

    choices = run_choices(tmp_path)

    assert [choice["value"] for choice in choices] == ["20260820_130000"]


def test_folder_selection_does_not_open_history_but_started_and_manual_runs_stay_selected():
    names = ["new", "old"]

    assert selected_run_value(names, None) is None
    assert selected_run_value(names, "old") == "old"
    assert selected_run_value(
        names,
        "old",
        action={"type": "folder"},
        triggered_ids={"action-store", "save-root-store"},
    ) is None
    assert selected_run_value(
        names,
        None,
        action={"type": "start", "run_name": "new"},
        triggered_ids={"poll"},
    ) == "new"


def test_latest_spectrum_uses_elapsed_time_not_directory_order(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    first = exports / "inline_00001_t00000.500s.csv"
    latest = exports / "inline_00011_t00020.500s.csv"
    first.write_text("first")
    latest.write_text("latest")

    assert latest_spectrum_path(tmp_path) == latest


def test_active_run_finds_a_running_worker(tmp_path):
    write_status(tmp_path / "finished", "complete")
    running = tmp_path / "current"
    write_status(running, "running")

    assert active_run(tmp_path) == running


def test_active_run_pointer_survives_dashboard_reloads(tmp_path):
    root = tmp_path / "chosen-runs"
    running = root / "current"
    write_status(running, "running")
    pointer = tmp_path / "state" / "active.json"

    remember_active_run(pointer, running)

    assert discover_active_run(pointer) == running.resolve()


def test_active_run_discovery_falls_back_to_the_legacy_default_root(tmp_path):
    running = tmp_path / "legacy-runs" / "current"
    write_status(running, "running")

    assert discover_active_run(
        tmp_path / "missing.json",
        fallback_roots=[tmp_path / "legacy-runs"],
    ) == running.resolve()


def test_worker_command_is_the_single_automatic_start_path(tmp_path):
    command = worker_command(
        python="/python",
        worker="/worker.py",
        interval_s=2.0,
        duration_minutes=10.0,
        run_dir=tmp_path / "run",
    )

    assert command == [
        "/python",
        "/worker.py",
        "--interval",
        "2.0",
        "--scan-time",
        "0.5",
        "--minutes",
        "10.0",
        "--run-dir",
        str(tmp_path / "run"),
    ]
    assert "--attach" not in command


def test_worker_command_supports_continuous_run(tmp_path):
    command = worker_command(
        python="/python",
        worker="/worker.py",
        interval_s=2.0,
        duration_minutes=None,
        run_dir=tmp_path / "run",
        continuous=True,
    )

    assert "--continuous" in command
    assert "--minutes" not in command


def test_move_run_to_trash_is_recoverable_and_rejects_outside_paths(tmp_path):
    root = tmp_path / "runs"
    run = root / "20260820_130000"
    run.mkdir(parents=True)
    (run / "status.json").write_text("{}")
    trash = tmp_path / "Trash"

    destination = move_run_to_trash(root, run.name, trash_root=trash)

    assert destination == trash / "PASCO_InFlow_20260820_130000"
    assert (destination / "status.json").exists()
    assert not run.exists()

    with pytest.raises(ValueError, match="Invalid run"):
        move_run_to_trash(root, "../outside", trash_root=trash)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (600, 600.0),
        (None, 520.0),
        (200, 379.8),
        (1000, 950.0),
    ],
)
def test_normalize_wavelength_keeps_slider_values_inside_the_measured_range(value, expected):
    assert normalize_wavelength(value, minimum_nm=379.8, maximum_nm=950.0) == expected


def test_start_preflight_only_validates_the_schedule():
    assert validate_start_settings(interval_s="2", duration_minutes="10") == (2.0, 10.0)
    assert validate_start_settings(interval_s=None, duration_minutes=None) == (2.0, 10.0)
    assert validate_start_settings(interval_s=1, duration_minutes=None) == (1.0, 10.0)

    with pytest.raises(ValueError, match="interval"):
        validate_start_settings(interval_s=0.5, duration_minutes=10)

    with pytest.raises(ValueError, match="duration"):
        validate_start_settings(interval_s=2, duration_minutes=0)


def test_start_preflight_allows_fractional_or_continuous_duration():
    assert validate_start_settings(interval_s=2, duration_minutes=2.75) == (2.0, 2.75)
    assert validate_start_settings(
        interval_s=2,
        duration_minutes=None,
        continuous=True,
    ) == (2.0, None)


def test_start_requires_an_explicit_existing_save_folder(tmp_path):
    assert validate_save_root(str(tmp_path)) == tmp_path.resolve()

    with pytest.raises(ValueError, match="Choose a save folder"):
        validate_save_root(None)

    file_path = tmp_path / "not-a-folder"
    file_path.write_text("x")
    with pytest.raises(ValueError, match="not a folder"):
        validate_save_root(str(file_path))


def test_start_stays_disabled_until_a_folder_is_confirmed_and_no_run_is_active(tmp_path):
    confirmed = {"path": str(tmp_path), "confirmed": True}

    assert start_is_disabled(None) is True
    assert start_is_disabled({"path": str(tmp_path), "confirmed": False}) is True
    assert start_is_disabled(confirmed) is False
    assert start_is_disabled(confirmed, active_run_dir=tmp_path / "active") is True


def test_save_root_falls_back_to_the_default_folder(tmp_path):
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    default = tmp_path / "default"

    assert resolve_save_root(str(chosen), default) == chosen.resolve()
    assert resolve_save_root(None, default) == default.resolve()
    assert default.is_dir()
    assert resolve_save_root(str(tmp_path / "missing"), default) == default.resolve()


def test_display_path_shortens_the_home_folder(tmp_path):
    assert display_path(Path.home() / "Desktop" / "PASCO_InFlow") == "~/Desktop/PASCO_InFlow"
    assert display_path("/Volumes/Data/runs") == "/Volumes/Data/runs"


def test_wavelength_colors_track_the_physical_spectrum():
    def channels(hex_color):
        return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))

    red, green, blue = channels(wavelength_color(520.0))
    assert green > 200 and green > red and green > blue

    red, green, blue = channels(wavelength_color(660.0))
    assert red > 200 and green < 60 and blue < 60

    assert wavelength_color(370.0) == wavelength_color(380.0)
    assert wavelength_color(1000.0) == wavelength_color(950.0)
    assert max(channels(wavelength_color(950.0))) < 130  # NIR fades toward dark

    vivid = channels(wavelength_color(580.0))
    plotted = channels(wavelength_plot_color(580.0))
    assert sum(plotted) < sum(vivid)  # legible on a white plot


def test_full_spectrum_click_selects_the_clicked_wavelength():
    assert clicked_wavelength({"points": [{"x": 557.83}]}) == pytest.approx(557.83)
    assert clicked_wavelength({"points": [{"x": 1200}]}) == 950.0
    assert clicked_wavelength(None) is None


def test_audit_log_is_compact_terminal_text():
    events = [
        {
            "wall_time": "2026-08-20T19:00:02.123",
            "elapsed_s": "2.500",
            "spectrum": "2",
            "phase": "saved",
            "detail": "Background CSV snapshot written",
            "filename": "inline_00002_t00002.500s.csv",
        }
    ]

    output = format_audit_log(events)

    assert "19:00:02.123" in output
    assert "+0002.500s" in output
    assert "[SAVED  ]" in output
    assert "#00002" in output
    assert "inline_00002_t00002.500s.csv" in output


def test_storage_estimate_scales_with_schedule_and_flags_big_runs():
    from pasco_dashboard_logic import storage_estimate

    modest = storage_estimate(2, 10)
    assert modest["text"] == "≈ 36 MB of data (300 spectra)"
    assert not modest["big"]

    big = storage_estimate(1, 600)
    assert big["big"]
    assert "GB" in big["text"] and "this run will get large" in big["text"]

    hourly = storage_estimate(1, None, continuous=True)
    assert "per hour" in hourly["text"] and "grows until stopped" in hourly["text"]

    assert storage_estimate(0, 10) is None
    assert storage_estimate(2, -5) is None
    assert storage_estimate("nope", 10) is None


def test_manual_record_runs_attach_without_any_ui_automation(tmp_path):
    from pasco_dashboard_logic import worker_command

    command = worker_command(
        python="/python",
        worker="/worker.py",
        interval_s=2.0,
        duration_minutes=None,
        run_dir=tmp_path / "run",
        continuous=True,
        attach=True,
    )

    assert "--attach" in command
