import json

from pasco_capture_worker import capture_live_spectra, close_recording_session, write_status


def test_status_can_include_the_run_directory_field(tmp_path):
    write_status(tmp_path, state="running", run_dir=str(tmp_path))

    assert json.loads((tmp_path / "status.json").read_text()) == {
        "state": "running",
        "run_dir": str(tmp_path),
    }


def test_live_spectra_are_sampled_without_restarting_pasaco():
    now = [100.0]
    events = []

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    def read_spectrum():
        events.append(("read", now[0]))
        return f"snapshot-{now[0]}"

    def save_spectrum(index, elapsed_s, snapshot):
        events.append(("save", index, elapsed_s, snapshot, now[0]))
        return f"spectrum-{index}.csv"

    captured = capture_live_spectra(
        count=2,
        interval_s=2.0,
        initial_settle_s=0.5,
        read_spectrum=read_spectrum,
        save_spectrum=save_spectrum,
        stop_requested=lambda: False,
        on_phase=lambda **_: None,
        clock=clock,
        sleep=sleep,
    )

    assert captured == ["spectrum-1.csv", "spectrum-2.csv"]
    assert events == [
        ("read", 100.5),
        ("save", 1, 0.5, "snapshot-100.5", 100.5),
        ("read", 102.5),
        ("save", 2, 2.5, "snapshot-102.5", 102.5),
    ]


def test_continuous_capture_runs_until_stop_is_requested():
    now = [0.0]
    captured_count = [0]

    def save_spectrum(index, elapsed_s, snapshot):
        captured_count[0] += 1
        return f"spectrum-{index}.csv"

    captured = capture_live_spectra(
        count=None,
        interval_s=1.0,
        initial_settle_s=0.5,
        read_spectrum=lambda: "snapshot",
        save_spectrum=save_spectrum,
        stop_requested=lambda: captured_count[0] >= 3,
        on_phase=lambda **_: None,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert captured == ["spectrum-1.csv", "spectrum-2.csv", "spectrum-3.csv"]


def test_stop_request_interrupts_the_wait_before_the_next_spectrum():
    now = [0.0]
    stopped = [False]
    reads = []

    def sleep(seconds):
        now[0] += seconds
        if now[0] >= 0.2:
            stopped[0] = True

    captured = capture_live_spectra(
        count=None,
        interval_s=2.0,
        initial_settle_s=0.5,
        read_spectrum=lambda: reads.append(now[0]),
        save_spectrum=lambda *_: "unexpected.csv",
        stop_requested=lambda: stopped[0],
        on_phase=lambda **_: None,
        clock=lambda: now[0],
        sleep=sleep,
    )

    assert captured == []
    assert reads == []
    assert now[0] < 0.5


def test_owned_recording_is_stopped_and_context_restored_exactly_once():
    events = []

    close_recording_session(
        recording_started=True,
        owns_recording=True,
        attached=False,
        original_process="Vivaldi",
        original_cursor=(700, 300),
        stop_owned=lambda timeout: events.append(("stop-owned", timeout)),
        restore_attached=lambda *_args: events.append(("restore-attached",)),
        restore_automatic=lambda *_args: events.append(("restore-automatic",)),
    )

    assert events == [("stop-owned", 6.0)]
