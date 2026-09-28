from types import SimpleNamespace

import pytest

from pasco_ui import (
    choose_directory,
    choose_save_path,
    click_at,
    export_current_spectrum,
    export_filename,
    export_menu_center,
    parse_position,
    run_applescript,
    spectrometry_running,
    wait_for_record_control,
)


def test_export_filename_encodes_index_and_elapsed_time():
    assert export_filename(index=3, elapsed_s=4.25) == "inline_00003_t00004.250s.csv"


def test_parse_position_accepts_applescript_list_format():
    assert parse_position("972, 164") == (972, 164)


def test_export_menu_center_tracks_the_share_button():
    assert export_menu_center((972, 164)) == (1027, 227)


def test_click_at_posts_a_real_move_down_up_mouse_sequence():
    events = []

    click_at(
        (120, 240),
        activate=lambda: events.append(("activate",)),
        post_event=lambda event_type, x, y: events.append((event_type, x, y)),
        sleep=lambda *_: None,
    )

    assert events == [
        ("activate",),
        ("move", 120, 240),
        ("down", 120, 240),
        ("up", 120, 240),
    ]


def test_export_current_spectrum_saves_the_live_inline_snapshot(tmp_path):
    events = []

    output = export_current_spectrum(
        output_dir=tmp_path,
        filename="inline_00001_t00000.000s.csv",
        open_export=lambda: events.append(("export",)),
        save_export=lambda directory, filename: events.append(("save", directory, filename)),
        wait_for_file=lambda path, timeout: path,
    )

    assert output == tmp_path / "inline_00001_t00000.000s.csv"
    assert events == [
        ("export",),
        ("save", tmp_path, "inline_00001_t00000.000s.csv"),
    ]


def test_wait_for_record_control_recovers_from_a_transient_missing_window():
    attempts = []
    now = [10.0]

    def probe():
        attempts.append(now[0])
        if len(attempts) < 3:
            raise RuntimeError("PASCO Record control not found")
        return "idle", (280, 861)

    def sleep(seconds):
        now[0] += seconds

    assert wait_for_record_control(
        timeout=1.0,
        probe=probe,
        clock=lambda: now[0],
        sleep=sleep,
    ) == ("idle", (280, 861))
    assert attempts == [10.0, 10.1, 10.2]


def test_run_applescript_has_a_hard_timeout():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="ready\n")

    assert run_applescript("return 1", timeout=2.5, runner=runner) == "ready"
    assert calls[0][1]["timeout"] == 2.5


def test_spectrometry_running_uses_a_non_accessibility_process_check():
    commands = []

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    assert spectrometry_running(runner=runner)
    assert commands == [(["pgrep", "-x", "Spectrometry"], {"capture_output": True, "timeout": 1.0})]


def test_choose_directory_returns_the_explicit_native_folder_choice(tmp_path):
    calls = []

    def run_script(script, *arguments, **kwargs):
        calls.append((script, arguments, kwargs))
        return str(tmp_path)

    assert choose_directory(initial_dir=tmp_path.parent, run_script=run_script) == tmp_path.resolve()
    assert "choose folder" in calls[0][0]
    assert calls[0][2]["timeout"] == 120.0


def test_choose_save_path_uses_a_native_save_as_dialog(tmp_path):
    destination = tmp_path / "trace.csv"
    calls = []

    def run_script(script, *arguments, **kwargs):
        calls.append((script, arguments, kwargs))
        return str(destination)

    assert choose_save_path(
        "trace.csv",
        initial_dir=tmp_path,
        run_script=run_script,
    ) == destination.resolve()
    assert "choose file name" in calls[0][0]
    assert calls[0][1][-1] == "trace.csv"


def test_set_recording_survives_a_slow_pasco_window(monkeypatch):
    import pasco_ui

    attempts = []

    def probe():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("PASCO Record control not found")
        return "idle", (100, 200)

    monkeypatch.setattr(pasco_ui, "record_control", probe)

    pasco_ui.set_recording(False, timeout=1.0)

    assert len(attempts) == 3


def test_set_recording_reports_a_human_readable_failure(monkeypatch):
    import pasco_ui

    def probe():
        raise RuntimeError("PASCO Record control not found")

    monkeypatch.setattr(pasco_ui, "record_control", probe)

    with pytest.raises(RuntimeError, match="Bring PASCO Spectrometry"):
        pasco_ui.set_recording(False, timeout=0.2)


def test_set_recording_stop_clicks_once_while_pasco_catches_up(monkeypatch):
    import pasco_ui

    now = [0.0]
    clicks = []

    def probe():
        state = "idle" if now[0] >= 2.0 else "recording"
        return state, (100, 200)

    monkeypatch.setattr(pasco_ui, "record_control", probe)
    monkeypatch.setattr(pasco_ui, "click_at", lambda position: clicks.append(position))
    monkeypatch.setattr(pasco_ui.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pasco_ui.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    pasco_ui.set_recording(False, timeout=3.0)

    assert clicks == [(100, 200)]


def test_set_recording_start_clicks_once_while_pasco_catches_up(monkeypatch):
    import pasco_ui

    now = [0.0]
    clicks = []

    def probe():
        state = "recording" if now[0] >= 2.0 else "idle"
        return state, (100, 200)

    monkeypatch.setattr(pasco_ui, "record_control", probe)
    monkeypatch.setattr(pasco_ui, "click_at", lambda position: clicks.append(position))
    monkeypatch.setattr(pasco_ui.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pasco_ui.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    pasco_ui.set_recording(True, timeout=3.0)

    assert clicks == [(100, 200)]


def test_start_gets_its_full_transition_timeout_after_the_control_appears(monkeypatch):
    import pasco_ui

    now = [0.0]
    clicks = []

    def wait_for_control(*_args, **_kwargs):
        now[0] = 1.5
        return "idle", (100, 200)

    def probe():
        return ("recording" if now[0] >= 4.0 else "idle"), (100, 200)

    monkeypatch.setattr(pasco_ui, "wait_for_record_control", wait_for_control)
    monkeypatch.setattr(pasco_ui, "record_control", probe)
    monkeypatch.setattr(pasco_ui, "click_at", lambda position: clicks.append(position))
    monkeypatch.setattr(pasco_ui.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pasco_ui.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    pasco_ui.set_recording(True, timeout=3.0)

    assert clicks == [(100, 200)]


def test_dashboard_stop_unhides_stops_verifies_and_restores_pasco(monkeypatch):
    import pasco_ui

    events = []
    monkeypatch.setattr(pasco_ui, "frontmost_process", lambda: "Vivaldi")
    monkeypatch.setattr(pasco_ui, "cursor_position", lambda: (700, 300))
    monkeypatch.setattr(pasco_ui, "set_spectrometry_hidden", lambda hidden: events.append(("hidden", hidden)))
    monkeypatch.setattr(pasco_ui, "activate_spectrometry", lambda: events.append(("activate",)))
    monkeypatch.setattr(pasco_ui.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    monkeypatch.setattr(pasco_ui, "set_recording", lambda active, timeout: events.append(("record", active, timeout)))
    monkeypatch.setattr(pasco_ui, "record_control", lambda: ("idle", (280, 861)))
    monkeypatch.setattr(
        pasco_ui,
        "restore_user_context",
        lambda process, cursor: events.append(("restore", process, cursor)),
    )

    assert pasco_ui.stop_recording_preserving_context(timeout=6.0) == "idle"
    assert events == [
        ("hidden", False),
        ("activate",),
        ("sleep", 0.4),
        ("record", False, 6.0),
        ("restore", "Vivaldi", (700, 300)),
    ]
