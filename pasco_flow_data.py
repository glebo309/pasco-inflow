#!/usr/bin/env python3
"""Parse PASCO exports and assemble inline spectral time series."""

# Deferred annotations keep this importable on the stock macOS Python 3.9
# that the double-click launcher may bootstrap from on a fresh MacBook.
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
import re
import time

import numpy as np
import pandas as pd


TIME_RE = re.compile(r"_t(?P<seconds>\d+(?:\.\d+)?)s(?:\.csv)?$", re.IGNORECASE)
RUN_RE = re.compile(r"Solution\s*#(?P<number>\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class PascoSpectrum:
    path: Path
    elapsed_s: float
    run_number: int | None
    wavelength_nm: np.ndarray
    absorbance_au: np.ndarray
    transmittance_pct: np.ndarray


def elapsed_from_filename(path):
    match = TIME_RE.search(Path(path).stem)
    if not match:
        raise ValueError(f"No elapsed time in PASCO filename: {Path(path).name}")
    return float(match.group("seconds"))


def _read_pasco_spectrum_file(path):
    path = Path(path)
    frame = pd.read_csv(path, usecols=[0, 1, 2])
    original_columns = list(frame.columns)
    run_match = RUN_RE.search(original_columns[1])
    run_number = int(run_match.group("number")) if run_match else None

    numeric = frame.apply(pd.to_numeric, errors="coerce").dropna(subset=[original_columns[0], original_columns[1]])
    return PascoSpectrum(
        path=path,
        elapsed_s=elapsed_from_filename(path),
        run_number=run_number,
        wavelength_nm=numeric[original_columns[0]].to_numpy(dtype=float),
        absorbance_au=numeric[original_columns[1]].to_numpy(dtype=float),
        transmittance_pct=numeric[original_columns[2]].to_numpy(dtype=float),
    )


@lru_cache(maxsize=4096)
def _read_pasco_spectrum_cached(path_text, modified_ns, file_size):
    del modified_ns, file_size
    return _read_pasco_spectrum_file(Path(path_text))


def read_pasco_spectrum(path, elapsed_s=None):
    path = Path(path)
    stat = path.stat()
    spectrum = _read_pasco_spectrum_cached(str(path), stat.st_mtime_ns, stat.st_size)
    if elapsed_s is None:
        return spectrum
    return replace(spectrum, elapsed_s=float(elapsed_s))


def _ordered_spectra(paths):
    return sorted((read_pasco_spectrum(path) for path in paths), key=lambda spectrum: spectrum.elapsed_s)


def consolidate_spectra(paths):
    spectra = _ordered_spectra(paths)
    if not spectra:
        return pd.DataFrame(columns=["wavelength_nm"])

    wavelength = spectra[0].wavelength_nm
    columns = {"wavelength_nm": wavelength}
    for spectrum in spectra:
        absorbance = np.interp(
            wavelength,
            spectrum.wavelength_nm,
            spectrum.absorbance_au,
            left=np.nan,
            right=np.nan,
        )
        columns[f"A_t{spectrum.elapsed_s:.3f}s"] = absorbance
    return pd.DataFrame(columns)


def absorbance_trace(paths, target_nm):
    spectra = _ordered_spectra(paths)
    rows = []
    for spectrum in spectra:
        absorbance = float(np.interp(target_nm, spectrum.wavelength_nm, spectrum.absorbance_au))
        rows.append(
            {
                "elapsed_s": spectrum.elapsed_s,
                "absorbance_au": absorbance,
                "wavelength_nm": float(target_nm),
                "source_file": spectrum.path.name,
            }
        )
    return pd.DataFrame(rows, columns=["elapsed_s", "absorbance_au", "wavelength_nm", "source_file"])


def continuous_absorbance_trace(paths, target_nm, *, interval_s, now_epoch=None):
    """Draw the newest measured segment progressively during the following interval."""
    ordered_paths = sorted((Path(path) for path in paths), key=elapsed_from_filename)
    trace = absorbance_trace(ordered_paths, target_nm)
    if len(trace) < 2:
        return trace

    interval_s = float(interval_s)
    if interval_s <= 0:
        raise ValueError("Continuous trace interval must be positive")
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    latest_arrival = ordered_paths[-1].stat().st_mtime
    progress = min(max((now_epoch - latest_arrival) / interval_s, 0.0), 1.0)

    visible = trace.iloc[:-1].copy()
    if progress <= 0:
        return visible.reset_index(drop=True)

    previous = trace.iloc[-2]
    latest = trace.iloc[-1]
    head = latest.copy()
    head["elapsed_s"] = previous["elapsed_s"] + progress * (
        latest["elapsed_s"] - previous["elapsed_s"]
    )
    head["absorbance_au"] = previous["absorbance_au"] + progress * (
        latest["absorbance_au"] - previous["absorbance_au"]
    )
    return pd.concat([visible, head.to_frame().T], ignore_index=True)


def continuous_spectrum_frame(paths, *, interval_s, now_epoch=None):
    """Move the displayed spectrum from the previous raw frame to the latest."""
    ordered_paths = sorted((Path(path) for path in paths), key=elapsed_from_filename)
    if not ordered_paths:
        raise ValueError("At least one spectrum is required")
    latest = read_pasco_spectrum(ordered_paths[-1])
    if len(ordered_paths) == 1:
        return latest

    interval_s = float(interval_s)
    if interval_s <= 0:
        raise ValueError("Continuous spectrum interval must be positive")
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    progress = min(
        max((now_epoch - ordered_paths[-1].stat().st_mtime) / interval_s, 0.0),
        1.0,
    )

    previous = read_pasco_spectrum(ordered_paths[-2])
    latest_absorbance = np.interp(
        previous.wavelength_nm,
        latest.wavelength_nm,
        latest.absorbance_au,
    )
    latest_transmittance = np.interp(
        previous.wavelength_nm,
        latest.wavelength_nm,
        latest.transmittance_pct,
    )
    return replace(
        latest,
        elapsed_s=previous.elapsed_s
        + progress * (latest.elapsed_s - previous.elapsed_s),
        wavelength_nm=previous.wavelength_nm,
        absorbance_au=previous.absorbance_au
        + progress * (latest_absorbance - previous.absorbance_au),
        transmittance_pct=previous.transmittance_pct
        + progress * (latest_transmittance - previous.transmittance_pct),
    )


def smoothed_absorbance_trace(paths, target_nm, *, window=3):
    """Return a centered display trace, intentionally one sample behind live data."""
    trace = absorbance_trace(paths, target_nm)
    window = int(window)
    if window < 1 or window % 2 == 0:
        raise ValueError("Smoothing window must be a positive odd number")
    if len(trace) < window or window == 1:
        return trace

    smoothed = trace["absorbance_au"].rolling(window=window, center=True).mean()
    result = trace.loc[smoothed.notna()].copy()
    result["absorbance_au"] = smoothed.dropna().to_numpy(dtype=float)
    if len(result) < 2:
        return trace
    return result.reset_index(drop=True)


def spectrum_fade_opacities(
    paths,
    *,
    interval_s,
    now_epoch=None,
    decay_intervals=2.5,
):
    """Opacity for raw spectra that begin fading only after a successor arrives."""
    ordered = sorted((Path(path) for path in paths), key=elapsed_from_filename)
    if not ordered:
        return []
    interval_s = float(interval_s)
    decay_duration = interval_s * float(decay_intervals)
    if interval_s <= 0 or decay_duration <= 0:
        raise ValueError("Spectrum fade duration must be positive")
    now_epoch = time.time() if now_epoch is None else float(now_epoch)

    opacities = []
    for index, _path in enumerate(ordered):
        if index == len(ordered) - 1:
            opacities.append(1.0)
            continue
        superseded_at = ordered[index + 1].stat().st_mtime
        age = max(0.0, now_epoch - superseded_at)
        opacities.append(max(0.0, 1.0 - age / decay_duration))
    return opacities


def _edge_moving_average(values, window):
    values = np.asarray(values, dtype=float)
    window = int(window)
    if window < 1 or window % 2 == 0:
        raise ValueError("Smoothing window must be a positive odd number")
    if window == 1 or len(values) < window:
        return values.copy()
    padding = window // 2
    padded = np.pad(values, padding, mode="edge")
    return np.convolve(padded, np.ones(window, dtype=float) / window, mode="valid")


def smoothed_latest_spectrum(paths, *, temporal_window=3, spectral_window=9):
    """Average recent frames and wavelength points for display, never for export."""
    spectra = _ordered_spectra(paths)
    if not spectra:
        raise ValueError("At least one spectrum is required")

    temporal_window = int(temporal_window)
    if temporal_window < 1 or temporal_window % 2 == 0:
        raise ValueError("Temporal smoothing window must be a positive odd number")
    history = spectra[-temporal_window:] if len(spectra) >= temporal_window else [spectra[-1]]
    reference = history[len(history) // 2]
    wavelength = reference.wavelength_nm
    absorbance = np.mean(
        [
            np.interp(wavelength, spectrum.wavelength_nm, spectrum.absorbance_au)
            for spectrum in history
        ],
        axis=0,
    )
    absorbance = _edge_moving_average(absorbance, spectral_window)
    return PascoSpectrum(
        path=reference.path,
        elapsed_s=reference.elapsed_s,
        run_number=reference.run_number,
        wavelength_nm=wavelength.copy(),
        absorbance_au=absorbance,
        transmittance_pct=100.0 * np.power(10.0, -absorbance),
    )
