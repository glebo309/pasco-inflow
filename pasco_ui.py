#!/usr/bin/env python3
"""macOS UI bridge for PASCO Spectrometry's validated acquisition workflow."""

import ctypes
from pathlib import Path
import re
import subprocess
import time


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


MOUSE_EVENT_TYPES = {
    "down": 1,
    "up": 2,
    "move": 5,
}


def export_filename(index, elapsed_s):
    return f"inline_{int(index):05d}_t{float(elapsed_s):09.3f}s.csv"


def parse_position(value):
    numbers = re.findall(r"-?\d+", value)
    if len(numbers) != 2:
        raise ValueError(f"Could not parse screen position: {value!r}")
    return int(numbers[0]), int(numbers[1])


def run_applescript(script, *arguments, timeout=3.0, runner=subprocess.run):
    result = runner(
        ["osascript", "-e", script, *[str(argument) for argument in arguments]],
        check=True,
        capture_output=True,
        text=True,
        timeout=float(timeout),
    )
    return result.stdout.strip()


def choose_directory(initial_dir=None, *, run_script=run_applescript):
    initial_dir = Path(initial_dir or (Path.home() / "Desktop")).expanduser().resolve()
    script = '''on run argv
  set startFolder to POSIX file (item 1 of argv) as alias
  set chosenFolder to choose folder with prompt "Choose where this PASCO run will be saved" default location startFolder
  return POSIX path of chosenFolder
end run'''
    try:
        selected = run_script(script, initial_dir, timeout=120.0)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1 and "-128" in (exc.stderr or ""):
            return None
        raise
    return Path(selected).expanduser().resolve() if selected else None


def choose_save_path(suggested_name, initial_dir=None, *, run_script=run_applescript):
    initial_dir = Path(initial_dir or (Path.home() / "Desktop")).expanduser().resolve()
    script = '''on run argv
  set startFolder to POSIX file (item 1 of argv) as alias
  set exportName to item 2 of argv
  set chosenFile to choose file name with prompt "Save PASCO export as" default location startFolder default name exportName
  return POSIX path of chosenFile
end run'''
    try:
        selected = run_script(script, initial_dir, suggested_name, timeout=120.0)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1 and "-128" in (exc.stderr or ""):
            return None
        raise
    return Path(selected).expanduser().resolve() if selected else None


def spectrometry_running(runner=subprocess.run):
    result = runner(
        ["pgrep", "-x", "Spectrometry"],
        capture_output=True,
        timeout=1.0,
    )
    return result.returncode == 0


def activate_spectrometry():
    run_applescript('tell application "Spectrometry" to activate')


def frontmost_process():
    script = '''tell application "System Events"
  return name of first application process whose frontmost is true
end tell'''
    return run_applescript(script)


def _post_core_graphics_event(event_type, x, y):
    core_graphics = ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    core_foundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    core_graphics.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        CGPoint,
        ctypes.c_uint32,
    ]
    core_graphics.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    core_graphics.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]

    event = core_graphics.CGEventCreateMouseEvent(
        None,
        MOUSE_EVENT_TYPES[event_type],
        CGPoint(float(x), float(y)),
        0,
    )
    if not event:
        raise RuntimeError("macOS could not create a mouse event")
    core_graphics.CGEventPost(0, event)
    core_foundation.CFRelease(event)


def cursor_position():
    core_graphics = ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    core_foundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    core_graphics.CGEventCreate.argtypes = [ctypes.c_void_p]
    core_graphics.CGEventCreate.restype = ctypes.c_void_p
    core_graphics.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    core_graphics.CGEventGetLocation.restype = CGPoint
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    event = core_graphics.CGEventCreate(None)
    if not event:
        raise RuntimeError("macOS could not read the cursor position")
    point = core_graphics.CGEventGetLocation(event)
    core_foundation.CFRelease(event)
    return int(point.x), int(point.y)


def set_spectrometry_hidden(hidden):
    visible = "false" if hidden else "true"
    run_applescript(
        f'''tell application "System Events"
  tell process "Spectrometry" to set visible to {visible}
end tell'''
    )


def restore_process_and_cursor(process_name, position):
    script = '''on run argv
  tell application "System Events"
    if exists process (item 1 of argv) then
      set frontmost of process (item 1 of argv) to true
    end if
  end tell
end run'''
    run_applescript(script, process_name)
    _post_core_graphics_event("move", int(position[0]), int(position[1]))


def restore_user_context(process_name, position):
    set_spectrometry_hidden(True)
    restore_process_and_cursor(process_name, position)


def click_at(
    position,
    activate=activate_spectrometry,
    post_event=None,
    sleep=time.sleep,
):
    """Send a genuine CoreGraphics click; PASCO ignores System Events clicks."""
    x, y = (int(position[0]), int(position[1]))
    post_event = post_event or _post_core_graphics_event
    activate()
    sleep(0.15)
    for event_type in ("move", "down", "up"):
        post_event(event_type, x, y)
        sleep(0.08)


def share_button_center():
    script = '''tell application "System Events"
  tell process "Spectrometry"
    set shareButton to first menu button of front window whose description is "Share"
    set buttonPosition to position of shareButton
    set buttonSize to size of shareButton
    return {((item 1 of buttonPosition) + ((item 1 of buttonSize) / 2) as integer), ((item 2 of buttonPosition) + ((item 2 of buttonSize) / 2) as integer)}
  end tell
end tell'''
    return parse_position(run_applescript(script))


def export_menu_center(share_center):
    """Center of PASCO's single Export Data item relative to Share."""
    return int(share_center[0]) + 55, int(share_center[1]) + 63


def record_control():
    script = '''tell application "System Events"
  tell process "Spectrometry"
    if exists (first checkbox of front window whose description is "Stop") then
      set recordButton to first checkbox of front window whose description is "Stop"
      set recordState to "recording"
    else if exists (first checkbox of front window whose description is "Record") then
      set recordButton to first checkbox of front window whose description is "Record"
      set recordState to "idle"
    else
      error "PASCO Record control not found"
    end if
    set buttonPosition to position of recordButton
    set buttonSize to size of recordButton
    set centerX to ((item 1 of buttonPosition) + ((item 1 of buttonSize) / 2) as integer)
    set centerY to ((item 2 of buttonPosition) + ((item 2 of buttonSize) / 2) as integer)
    return recordState & ", " & centerX & ", " & centerY
  end tell
end tell'''
    response = run_applescript(script)
    state, coordinates = response.split(",", 1)
    return state.strip(), parse_position(coordinates)


def wait_for_record_control(
    timeout=2.0,
    *,
    probe=record_control,
    clock=time.monotonic,
    sleep=time.sleep,
):
    """Wait through PASCO's brief window/control transitions before failing."""
    deadline = clock() + float(timeout)
    while True:
        try:
            return probe()
        except Exception:
            remaining = deadline - clock()
            if remaining <= 0:
                raise
            sleep(min(0.1, remaining))


def set_recording(active, timeout=3.0):
    desired_state = "recording" if active else "idle"
    try:
        # PASCO's window can take a moment to expose its controls right after
        # being unhidden or activated, so probe with patience.
        current_state, position = wait_for_record_control(
            timeout,
            probe=lambda: record_control(),
        )
    except Exception as exc:
        raise RuntimeError(
            "PASCO's Record button was not reachable. Bring PASCO Spectrometry "
            "to the front, close any open dialog, and start the run again."
        ) from exc
    if current_state == desired_state:
        return

    deadline = time.monotonic() + timeout
    # Record is a toggle in both directions. Never re-click while PASCO is
    # still changing its accessibility label: that can undo the first click.
    click_at(position)
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            current_state, _ = record_control()
        except Exception:
            continue
        if current_state == desired_state:
            return
    raise TimeoutError(f"PASCO did not enter {desired_state} state")


def stop_recording_preserving_context(timeout=6.0):
    """Stop PASCO now, verify Record is off, then return the user to their app."""
    original_process = frontmost_process()
    original_cursor = cursor_position()
    try:
        set_spectrometry_hidden(False)
        activate_spectrometry()
        time.sleep(0.4)
        set_recording(False, timeout=timeout)
        state, _ = record_control()
        if state != "idle":
            raise RuntimeError(f"PASCO remained {state}")
        return state
    finally:
        restore_user_context(original_process, original_cursor)


def export_dialog_open():
    script = '''tell application "System Events"
  tell process "Spectrometry"
    return (exists button "Save" of front window) and (exists text field "Save As:" of front window)
  end tell
end tell'''
    return run_applescript(script).lower() == "true"


def wait_for_export_dialog(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if export_dialog_open():
            return True
        time.sleep(0.05)
    raise TimeoutError("PASCO Export Data dialog did not open")


def open_export_dialog(export_menu_position=None):
    share_center = share_button_center()
    click_at(share_center)
    time.sleep(0.35)
    click_at(export_menu_position or export_menu_center(share_center))
    wait_for_export_dialog()


def save_export_dialog(output_dir, filename):
    output_dir = str(Path(output_dir).resolve())
    script = r'''on run argv
  set destinationFolder to item 1 of argv
  set exportName to item 2 of argv
  tell application "Spectrometry" to activate
  tell application "System Events"
    tell process "Spectrometry"
      keystroke "g" using {command down, shift down}
      delay 0.3
      keystroke destinationFolder
      key code 36
      delay 0.4
      set value of text field "Save As:" of front window to exportName
      click button "Save" of front window
    end tell
  end tell
end run'''
    run_applescript(script, output_dir, filename)


def wait_for_export_file(path, timeout=8.0):
    path = Path(path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return path
        time.sleep(0.05)
    raise TimeoutError(f"PASCO export was not written: {path}")


def export_current_spectrum(
    output_dir,
    filename,
    open_export=open_export_dialog,
    save_export=save_export_dialog,
    wait_for_file=wait_for_export_file,
):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename

    open_export()
    save_export(output_dir, filename)
    return wait_for_file(output_path, timeout=8.0)
