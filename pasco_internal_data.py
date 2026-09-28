#!/usr/bin/env python3
"""Read PASCO's own calibrated live spectrum from its memory-backed data files."""

import csv
from dataclasses import dataclass
from pathlib import Path
import struct
import subprocess
import time

import numpy as np


RECORD = struct.Struct("<Id")


def read_segment(path):
    data = Path(path).read_bytes()
    if len(data) % RECORD.size:
        raise ValueError(f"Invalid PASCO data-segment size: {len(data)}")
    return np.asarray(
        [value for valid, value in RECORD.iter_unpack(data) if valid == 1],
        dtype=float,
    )


@dataclass(frozen=True)
class LiveSpectrumFiles:
    wavelength_path: Path
    absorbance_path: Path
    transmittance_path: Path

    def read(self):
        wavelength = read_segment(self.wavelength_path)
        absorbance = read_segment(self.absorbance_path)
        transmittance = read_segment(self.transmittance_path)
        if not (len(wavelength) == len(absorbance) == len(transmittance)):
            raise ValueError("PASCO updated its live arrays during the read; retrying is required")
        return wavelength, absorbance, transmittance


def _is_wavelength(values):
    return (
        len(values) >= 3
        and 350.0 < values[0] < 450.0
        and 850.0 < values[-1] < 1000.0
        and np.all(np.diff(values) > 0)
    )


def _file_sequence(path):
    try:
        return int(Path(path).stem.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 0


def classify_live_files(paths):
    arrays = {}
    for path in map(Path, paths):
        try:
            values = read_segment(path)
        except (OSError, ValueError):
            continue
        if len(values) >= 3 and np.all(np.isfinite(values)):
            arrays[path] = values

    wavelength_candidates = [path for path, values in arrays.items() if _is_wavelength(values)]
    if not wavelength_candidates:
        raise ValueError("PASCO calibrated wavelength array not found")

    best_pair = None
    best_error = float("inf")
    best_sequence = -1
    non_wavelength = [path for path in arrays if path not in wavelength_candidates]
    for absorbance_path in non_wavelength:
        absorbance = arrays[absorbance_path]
        if absorbance.min() < -0.5 or absorbance.max() > 10.0:
            continue
        expected_transmittance = 100.0 * np.power(10.0, -absorbance)
        for transmittance_path in non_wavelength:
            if transmittance_path == absorbance_path:
                continue
            transmittance = arrays[transmittance_path]
            if len(transmittance) != len(absorbance):
                continue
            if transmittance.min() < 0.0 or transmittance.max() > 100.5:
                continue
            error = float(np.max(np.abs(transmittance - expected_transmittance)))
            sequence = max(_file_sequence(absorbance_path), _file_sequence(transmittance_path))
            if error < best_error - 1e-12 or (
                abs(error - best_error) <= 1e-12 and sequence > best_sequence
            ):
                best_error = error
                best_sequence = sequence
                best_pair = absorbance_path, transmittance_path

    if best_pair is None or best_error > 1e-6:
        raise ValueError("PASCO calibrated absorbance/transmittance pair not found")

    absorbance_path, transmittance_path = best_pair
    point_count = len(arrays[absorbance_path])
    matching_wavelengths = [
            path
            for path in wavelength_candidates
            if len(arrays[path]) == point_count
    ]
    if not matching_wavelengths:
        raise ValueError("PASCO wavelength and absorbance arrays have different lengths")
    wavelength_path = max(matching_wavelengths, key=_file_sequence)

    return LiveSpectrumFiles(
        wavelength_path=wavelength_path,
        absorbance_path=absorbance_path,
        transmittance_path=transmittance_path,
    )


def read_with_rediscovery(source, rediscover, attempts=5, sleep=time.sleep):
    last_error = None
    for attempt in range(attempts):
        try:
            return source.read(), source
        except (OSError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                source = rediscover()
                sleep(0.02)
    raise RuntimeError(f"Could not read a stable PASCO live spectrum: {last_error}")


def pasco_data_directory():
    result = subprocess.run(
        ["pgrep", "-x", "Spectrometry"],
        check=True,
        capture_output=True,
        text=True,
    )
    pids = [int(value) for value in result.stdout.split()]
    if not pids:
        raise RuntimeError("PASCO Spectrometry is not running")
    base = (
        Path.home()
        / "Library"
        / "Application Support"
        / "PASCO scientific"
        / "Spectrometry"
        / "Spectrometry"
    )
    for pid in reversed(pids):
        path = base / f"Spectrometry_{pid}" / "data"
        if path.is_dir():
            return path
    raise RuntimeError("PASCO live data directory was not found")


def wait_for_live_files(data_directory, baseline_paths, timeout=10.0):
    data_directory = Path(data_directory)
    baseline_paths = {Path(path) for path in baseline_paths}
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        current_paths = set(data_directory.glob("*.tmp")) - baseline_paths
        try:
            return classify_live_files(current_paths)
        except ValueError as exc:
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(f"PASCO live calibrated arrays did not appear: {last_error}")


def live_data_age_s(data_directory=None):
    """Seconds since PASCO last updated its live arrays, or None when they
    are unavailable. A recording PASCO rewrites them continuously, so a small
    age means the red Record button is on. Pure filesystem: works without any
    accessibility permission on every Mac."""
    try:
        directory = (
            Path(data_directory) if data_directory is not None else pasco_data_directory()
        )
        newest = max(path.stat().st_mtime for path in directory.glob("*.tmp"))
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError):
        return None
    return max(time.time() - newest, 0.0)


def write_live_csv(path, *, wavelength_nm, absorbance_au, transmittance_pct):
    """Write atomically: the dashboard globs this folder every poll, so a
    partially written file must never be visible under the final name."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Wavelength (nm)",
                "Absorbance PASCO calibrated",
                "Transmittance PASCO calibrated",
            ]
        )
        writer.writerows(zip(wavelength_nm, absorbance_au, transmittance_pct))
    temporary.replace(path)
    return path
