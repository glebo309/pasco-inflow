#!/usr/bin/env python3
"""Background worker for PASCO's continuously updated calibrated live spectrum."""

import argparse
import csv
from datetime import datetime
import itertools
import json
from pathlib import Path
import time

from pasco_autoclick import validate_run_settings
from pasco_flow_data import consolidate_spectra
from pasco_internal_data import (
    pasco_data_directory,
    read_with_rediscovery,
    wait_for_live_files,
    write_live_csv,
)
from pasco_ui import (
    activate_spectrometry,
    cursor_position,
    frontmost_process,
    restore_process_and_cursor,
    restore_user_context,
    set_recording as set_pasco_recording,
    set_spectrometry_hidden,
    stop_recording_preserving_context,
)


def write_status(directory, **status):
    path = Path(directory) / "status.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(status, indent=2) + "\n")
    temporary.replace(path)


def append_event(run_dir, *, elapsed_s, spectrum_index, phase, detail="", filename=""):
    path = Path(run_dir) / "events.csv"
    new_file = not path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(["wall_time", "elapsed_s", "spectrum", "phase", "detail", "filename"])
        writer.writerow(
            [
                datetime.now().isoformat(timespec="milliseconds"),
                f"{float(elapsed_s):.3f}",
                spectrum_index,
                phase,
                detail,
                filename,
            ]
        )


def close_recording_session(
    *,
    recording_started,
    owns_recording,
    attached,
    original_process,
    original_cursor,
    stop_owned=stop_recording_preserving_context,
    restore_attached=restore_process_and_cursor,
    restore_automatic=restore_user_context,
):
    """Stop and restore once; never duplicate the dashboard's app switching."""
    if recording_started and owns_recording:
        try:
            stop_owned(timeout=6.0)
        except Exception:
            pass
        return
    if original_process is None or original_cursor is None:
        return
    try:
        if attached:
            set_spectrometry_hidden(False)
            restore_attached(original_process, original_cursor)
        else:
            restore_automatic(original_process, original_cursor)
    except Exception:
        pass


def capture_live_spectra(
    *,
    count,
    interval_s,
    initial_settle_s,
    read_spectrum,
    save_spectrum,
    stop_requested,
    on_phase,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """Snapshot one continuously recording PASCO solution without restarting it."""
    start = clock()
    captured = []
    indices = itertools.count(1) if count is None else range(1, count + 1)
    for index in indices:
        if stop_requested():
            break

        deadline = start + initial_settle_s + (index - 1) * interval_s
        while True:
            if stop_requested():
                return captured
            remaining = deadline - clock()
            if remaining <= 0:
                break
            sleep(min(0.1, remaining))

        if stop_requested():
            return captured

        elapsed_s = clock() - start
        on_phase(phase="sampling", spectrum_index=index, elapsed_s=elapsed_s)
        snapshot = read_spectrum()
        output_path = save_spectrum(index, elapsed_s, snapshot)
        captured.append(output_path)
        on_phase(
            phase="saved",
            spectrum_index=index,
            elapsed_s=elapsed_s,
            filename=Path(output_path).name,
        )
    return captured


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, required=True)
    parser.add_argument("--scan-time", type=float, default=0.5, help="Initial live-spectrum settle time")
    parser.add_argument("--minutes", type=float)
    parser.add_argument("--continuous", action="store_true", help="Record until the STOP file is created")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--attach", action="store_true", help="Attach to PASCO when Record is already active")
    args = parser.parse_args()

    interval_s, initial_settle_s, count = validate_run_settings(
        args.interval,
        args.scan_time,
        args.minutes,
        continuous=args.continuous,
    )
    run_dir = args.run_dir.resolve()
    spectra_dir = run_dir / "exports"
    spectra_dir.mkdir(parents=True, exist_ok=True)
    stop_path = run_dir / "STOP"
    manifest_path = run_dir / "manifest.csv"

    captured_files = []
    worker_start = time.monotonic()
    latest_file = ""
    recording_started = False
    owns_recording = False
    original_process = None
    original_cursor = None

    write_status(
        run_dir,
        state="running",
        phase="starting",
        spectra_captured=0,
        spectra_planned=count,
        interval_s=interval_s,
        scan_time_s=initial_settle_s,
        acquisition="PASCO calibrated live data",
        control_mode="attach" if args.attach else "automatic",
        run_dir=str(run_dir),
    )

    try:
        data_directory = pasco_data_directory()
        if args.attach:
            # Attach never touches PASCO: no accessibility probes, no window
            # switching, no cursor. Whether Record is on shows up as live
            # data below, and its absence produces a plain-language error.
            baseline_paths = set()
            recording_started = True
            append_event(
                run_dir,
                elapsed_s=0,
                spectrum_index=0,
                phase="attaching",
                detail="Reading the Solution recording in PASCO",
            )
        else:
            original_process = frontmost_process()
            original_cursor = cursor_position()
            set_spectrometry_hidden(False)
            activate_spectrometry()
            time.sleep(0.4)  # let the unhidden window expose its controls
            set_pasco_recording(False)
            baseline_paths = set(data_directory.glob("*.tmp"))
            append_event(
                run_dir,
                elapsed_s=0,
                spectrum_index=0,
                phase="starting",
                detail="Starting one continuous PASCO Solution",
            )
            set_pasco_recording(True)
            recording_started = True
            owns_recording = True

        try:
            live_files = wait_for_live_files(data_directory, baseline_paths)
        except TimeoutError as exc:
            if args.attach:
                raise RuntimeError(
                    "No live data from PASCO. Press the red Record button in "
                    "PASCO, then press Start run again."
                ) from exc
            raise
        metadata = {
            "source": "PASCO Spectrometry calibrated live data storage",
            "data_directory": str(data_directory),
            "wavelength_file": str(live_files.wavelength_path),
            "absorbance_file": str(live_files.absorbance_path),
            "transmittance_file": str(live_files.transmittance_path),
            "validated_relation": "T_percent = 100 * 10^(-A)",
        }
        (run_dir / "source_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

        if not args.attach:
            restore_user_context(original_process, original_cursor)
        append_event(
            run_dir,
            elapsed_s=time.monotonic() - worker_start,
            spectrum_index=0,
            phase="recording",
            detail="PASCO hidden; calibrated spectrum updating continuously in background",
        )

        with manifest_path.open("w", newline="") as manifest_handle:
            manifest = csv.writer(manifest_handle)
            manifest.writerow(["index", "elapsed_s", "wall_time", "filename"])
            manifest_handle.flush()

            def read_spectrum():
                nonlocal live_files
                snapshot, live_files = read_with_rediscovery(
                    live_files,
                    rediscover=lambda: wait_for_live_files(
                        data_directory,
                        baseline_paths,
                        timeout=2.0,
                    ),
                )
                return snapshot

            def save_spectrum(index, elapsed_s, snapshot):
                nonlocal latest_file
                wavelength, absorbance, transmittance = snapshot
                filename = f"inline_{index:05d}_t{elapsed_s:09.3f}s.csv"
                output_path = write_live_csv(
                    spectra_dir / filename,
                    wavelength_nm=wavelength,
                    absorbance_au=absorbance,
                    transmittance_pct=transmittance,
                )
                captured_files.append(output_path)
                latest_file = filename
                manifest.writerow(
                    [
                        index,
                        f"{elapsed_s:.6f}",
                        datetime.now().isoformat(timespec="milliseconds"),
                        filename,
                    ]
                )
                manifest_handle.flush()
                consolidate_spectra(captured_files).to_csv(
                    run_dir / "consolidated_spectra.csv",
                    index=False,
                )
                return output_path

            def on_phase(*, phase, spectrum_index, elapsed_s, filename=""):
                detail = {
                    "sampling": "Reading PASCO calibrated live arrays",
                    "saved": "Background CSV snapshot written and consolidated",
                }.get(phase, "")
                append_event(
                    run_dir,
                    elapsed_s=elapsed_s,
                    spectrum_index=spectrum_index,
                    phase=phase,
                    detail=detail,
                    filename=filename,
                )
                write_status(
                    run_dir,
                    state="running",
                    phase=phase,
                    spectra_captured=len(captured_files),
                    spectra_planned=count,
                    elapsed_s=elapsed_s,
                    interval_s=interval_s,
                    scan_time_s=initial_settle_s,
                    latest_file=filename or latest_file,
                    acquisition="PASCO calibrated live data",
                    control_mode="attach" if args.attach else "automatic",
                    pasco_window="hidden",
                    run_dir=str(run_dir),
                )

            capture_live_spectra(
                count=count,
                interval_s=interval_s,
                initial_settle_s=initial_settle_s,
                read_spectrum=read_spectrum,
                save_spectrum=save_spectrum,
                stop_requested=stop_path.exists,
                on_phase=on_phase,
            )

        final_state = "stopped" if stop_path.exists() else "complete"
        append_event(
            run_dir,
            elapsed_s=time.monotonic() - worker_start,
            spectrum_index=len(captured_files),
            phase=final_state,
            detail=f"Run {final_state}; {len(captured_files)} calibrated spectra saved",
        )
        write_status(
            run_dir,
            state=final_state,
            phase=final_state,
            spectra_captured=len(captured_files),
            spectra_planned=count,
            elapsed_s=time.monotonic() - worker_start,
            interval_s=interval_s,
            scan_time_s=initial_settle_s,
            latest_file=latest_file,
            acquisition="PASCO calibrated live data",
            control_mode="attach" if args.attach else "automatic",
            run_dir=str(run_dir),
        )
    except Exception as exc:
        append_event(
            run_dir,
            elapsed_s=time.monotonic() - worker_start,
            spectrum_index=len(captured_files),
            phase="error",
            detail=f"{type(exc).__name__}: {exc}",
        )
        write_status(
            run_dir,
            state="error",
            phase="error",
            spectra_captured=len(captured_files),
            spectra_planned=count,
            latest_file=latest_file,
            error=f"{type(exc).__name__}: {exc}",
            run_dir=str(run_dir),
        )
        raise
    finally:
        close_recording_session(
            recording_started=recording_started,
            owns_recording=owns_recording,
            attached=args.attach,
            original_process=original_process,
            original_cursor=original_cursor,
        )


if __name__ == "__main__":
    main()
