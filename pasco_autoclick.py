#!/usr/bin/env python3
"""Click PASCO Spectrometry's manual next-sample control on a fixed schedule."""

import argparse
import math
import subprocess
import time


def click_deadlines(start, interval_s, count):
    return [float(start) + index * float(interval_s) for index in range(int(count))]


def validate_run_settings(interval_s, scan_time_s, minutes=None, continuous=False):
    interval_s = float(interval_s)
    scan_time_s = float(scan_time_s)
    if interval_s <= 0:
        raise ValueError("The interval must be greater than zero.")
    if scan_time_s <= 0 or scan_time_s >= interval_s:
        raise ValueError("The scan time must be greater than zero and shorter than the interval.")
    if continuous:
        return interval_s, scan_time_s, None
    minutes = float(minutes)
    if minutes <= 0:
        raise ValueError("The run duration must be greater than zero.")
    return interval_s, scan_time_s, max(1, math.ceil(minutes * 60.0 / interval_s))


def cursor_position():
    source = (
        "import CoreGraphics; "
        "if let event = CGEvent(source: nil) { "
        "let point = event.location; "
        'print("\\(Int(point.x)) \\(Int(point.y))") }'
    )
    result = subprocess.run(
        ["swift", "-e", source],
        check=True,
        capture_output=True,
        text=True,
    )
    x_text, y_text = result.stdout.strip().split()
    return int(x_text), int(y_text)


def click_spectrometry(x, y):
    script = f'''tell application "Spectrometry" to activate
delay 0.1
tell application "System Events" to click at {{{int(x)}, {int(y)}}}'''
    subprocess.run(["osascript", "-e", script], check=True)


def run_measurements(
    interval_s,
    scan_time_s,
    count,
    click,
    clock=time.monotonic,
    sleep=time.sleep,
    report=print,
):
    start = clock()
    elapsed_times = []
    for index, deadline in enumerate(click_deadlines(start, interval_s, count), start=1):
        remaining = deadline - clock()
        if remaining > 0:
            sleep(remaining)
        elapsed = clock() - start
        click()
        stop_deadline = deadline + scan_time_s
        remaining = stop_deadline - clock()
        if remaining > 0:
            sleep(remaining)
        click()
        elapsed_times.append(elapsed)
        report(f"Spectrum {index}/{count}, t={elapsed:.3f}s")
    return elapsed_times


def prompt_number(label, default):
    value = input(f"{label} [{default}]: ").strip()
    return float(value) if value else float(default)


def main():
    parser = argparse.ArgumentParser(
        description="Automatically click PASCO Spectrometry's next-sample button."
    )
    parser.add_argument("--interval", type=float, help="Seconds between spectra")
    parser.add_argument("--scan-time", type=float, default=0.5, help="Seconds between Start and Stop")
    parser.add_argument("--minutes", type=float, help="Total run time in minutes")
    parser.add_argument("--countdown", type=int, default=8, help="Seconds to position the pointer")
    args = parser.parse_args()

    interval = args.interval if args.interval is not None else prompt_number("Seconds between spectra", 2)
    minutes = args.minutes if args.minutes is not None else prompt_number("Run duration in minutes", 10)
    interval, scan_time, count = validate_run_settings(interval, args.scan_time, minutes)

    subprocess.run(["open", "-a", "Spectrometry"], check=True)
    print()
    print("Prepare and calibrate the experiment in the official PASCO application.")
    print("Put the mouse pointer exactly over the button you normally click for the next sample.")
    print("Do not move the PASCO window after the position is captured.")
    print()
    for remaining in range(max(0, args.countdown), 0, -1):
        print(f"Capturing button position in {remaining}...", end="\r", flush=True)
        time.sleep(1)

    x, y = cursor_position()
    print(f"Button captured at screen position {x}, {y}.{' ' * 20}")
    print(f"Recording {count} inline spectra over {minutes:g} min, one every {interval:g} s.")
    print(f"Each time point records for {scan_time:g} s, then stops and keeps the spectrum.")
    print("Switch back to this Terminal window and press Control-C to stop early.")
    time.sleep(1)

    try:
        run_measurements(
            interval_s=interval,
            scan_time_s=scan_time,
            count=count,
            click=lambda: click_spectrometry(x, y),
        )
    except KeyboardInterrupt:
        print("\nStopped early.")
    except subprocess.CalledProcessError:
        print("\nClick failed. Allow Terminal to control the computer in System Settings, Privacy & Security, Accessibility.")
        raise SystemExit(1)
    else:
        print("Run complete.")


if __name__ == "__main__":
    main()
