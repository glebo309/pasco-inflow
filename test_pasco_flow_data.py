from pathlib import Path
import os

import pytest

from pasco_flow_data import (
    absorbance_trace,
    consolidate_spectra,
    continuous_absorbance_trace,
    continuous_spectrum_frame,
    read_pasco_spectrum,
    smoothed_absorbance_trace,
    smoothed_latest_spectrum,
    spectrum_fade_opacities,
)


def write_pasco_csv(path: Path, run_number: int, absorbance_values):
    path.write_text(
        "Wavelength (nm),Absorbance Solution #"
        f"{run_number},Transmittance Solution #{run_number},,Untitled.sp,,,Solution #{run_number},\n"
        f"400.0,{absorbance_values[0]},90.0,,2026-08-20 16:12:49,,Analysis Wavelength,,\n"
        f"500.0,{absorbance_values[1]},80.0,,Analyze Solution,,Integration Time,10 ms,\n"
        f"600.0,{absorbance_values[2]},70.0\n"
    )


def test_read_pasco_spectrum_ignores_embedded_metadata(tmp_path):
    path = tmp_path / "inline_00001_t0000.000s.csv"
    write_pasco_csv(path, 1, [0.1, 0.2, 0.3])

    spectrum = read_pasco_spectrum(path)

    assert spectrum.elapsed_s == 0.0
    assert spectrum.run_number == 1
    assert spectrum.wavelength_nm.tolist() == [400.0, 500.0, 600.0]
    assert spectrum.absorbance_au.tolist() == [0.1, 0.2, 0.3]


def test_consolidate_spectra_creates_one_column_per_inline_timepoint(tmp_path):
    first = tmp_path / "inline_00001_t0000.000s.csv"
    second = tmp_path / "inline_00002_t0002.000s.csv"
    write_pasco_csv(first, 1, [0.1, 0.2, 0.3])
    write_pasco_csv(second, 2, [0.2, 0.4, 0.6])

    matrix = consolidate_spectra([second, first])

    assert matrix.columns.tolist() == ["wavelength_nm", "A_t0.000s", "A_t2.000s"]
    assert matrix["A_t2.000s"].tolist() == [0.2, 0.4, 0.6]


def test_absorbance_trace_interpolates_selected_wavelength(tmp_path):
    first = tmp_path / "inline_00001_t0000.000s.csv"
    second = tmp_path / "inline_00002_t0002.000s.csv"
    write_pasco_csv(first, 1, [0.1, 0.2, 0.3])
    write_pasco_csv(second, 2, [0.2, 0.4, 0.6])

    trace = absorbance_trace([first, second], target_nm=550.0)

    assert trace["elapsed_s"].tolist() == [0.0, 2.0]
    assert trace["absorbance_au"].tolist() == pytest.approx([0.25, 0.5])


def test_smoothed_trace_is_centered_and_lags_by_one_measurement(tmp_path):
    paths = []
    for index, absorbance in enumerate([1, 2, 3, 4, 5]):
        path = tmp_path / f"inline_{index + 1:05d}_t{index:04d}.000s.csv"
        write_pasco_csv(path, index + 1, [absorbance] * 3)
        paths.append(path)

    trace = smoothed_absorbance_trace(paths, target_nm=500.0, window=3)

    assert trace["elapsed_s"].tolist() == [1.0, 2.0, 3.0]
    assert trace["absorbance_au"].tolist() == pytest.approx([2.0, 3.0, 4.0])


def test_smoothed_latest_spectrum_temporally_averages_last_three_frames(tmp_path):
    paths = []
    for index, absorbance in enumerate([1, 2, 6, 10]):
        path = tmp_path / f"inline_{index + 1:05d}_t{index:04d}.000s.csv"
        write_pasco_csv(path, index + 1, [absorbance] * 3)
        paths.append(path)

    spectrum = smoothed_latest_spectrum(paths, temporal_window=3, spectral_window=1)

    assert spectrum.elapsed_s == 2.0
    assert spectrum.absorbance_au.tolist() == pytest.approx([6.0, 6.0, 6.0])


def test_smoothed_trace_keeps_raw_line_until_two_smoothed_points_exist(tmp_path):
    paths = []
    for index, absorbance in enumerate([1, 2, 3]):
        path = tmp_path / f"inline_{index + 1:05d}_t{index:04d}.000s.csv"
        write_pasco_csv(path, index + 1, [absorbance] * 3)
        paths.append(path)

    trace = smoothed_absorbance_trace(paths, target_nm=500.0, window=3)

    assert trace["elapsed_s"].tolist() == [0.0, 1.0, 2.0]
    assert trace["absorbance_au"].tolist() == pytest.approx([1.0, 2.0, 3.0])


def test_spectrum_fade_starts_when_successor_arrives_and_lasts_2_5_intervals(tmp_path):
    paths = []
    for index, modified_at in enumerate([100.0, 102.0, 104.0]):
        path = tmp_path / f"inline_{index + 1:05d}_t{index * 2:04d}.000s.csv"
        path.touch()
        os.utime(path, (modified_at, modified_at))
        paths.append(path)

    opacities = spectrum_fade_opacities(
        paths,
        interval_s=2.0,
        now_epoch=105.0,
        decay_intervals=2.5,
    )

    assert opacities == pytest.approx([0.4, 0.8, 1.0])


def test_continuous_trace_draws_the_latest_raw_segment_over_the_next_interval(tmp_path):
    paths = []
    for index, absorbance in enumerate([1, 3, 7]):
        path = tmp_path / f"inline_{index + 1:05d}_t{index * 2:04d}.000s.csv"
        write_pasco_csv(path, index + 1, [absorbance] * 3)
        paths.append(path)
    os.utime(paths[-1], (100.0, 100.0))

    trace = continuous_absorbance_trace(
        paths,
        target_nm=500.0,
        interval_s=2.0,
        now_epoch=101.0,
    )

    assert trace["elapsed_s"].tolist() == pytest.approx([0.0, 2.0, 3.0])
    assert trace["absorbance_au"].tolist() == pytest.approx([1.0, 3.0, 5.0])


def test_continuous_trace_reaches_the_latest_raw_point_without_extrapolating(tmp_path):
    paths = []
    for index, absorbance in enumerate([1, 3, 7]):
        path = tmp_path / f"inline_{index + 1:05d}_t{index * 2:04d}.000s.csv"
        write_pasco_csv(path, index + 1, [absorbance] * 3)
        paths.append(path)
    os.utime(paths[-1], (100.0, 100.0))

    trace = continuous_absorbance_trace(
        paths,
        target_nm=500.0,
        interval_s=2.0,
        now_epoch=105.0,
    )

    assert trace["elapsed_s"].tolist() == pytest.approx([0.0, 2.0, 4.0])
    assert trace["absorbance_au"].tolist() == pytest.approx([1.0, 3.0, 7.0])


def test_continuous_spectrum_moves_between_the_two_latest_raw_frames(tmp_path):
    previous = tmp_path / "inline_00001_t0000.000s.csv"
    latest = tmp_path / "inline_00002_t0002.000s.csv"
    write_pasco_csv(previous, 1, [1.0, 2.0, 3.0])
    write_pasco_csv(latest, 2, [3.0, 6.0, 9.0])
    os.utime(latest, (100.0, 100.0))

    frame = continuous_spectrum_frame(
        [previous, latest],
        interval_s=2.0,
        now_epoch=101.0,
    )

    assert frame.elapsed_s == pytest.approx(1.0)
    assert frame.absorbance_au.tolist() == pytest.approx([2.0, 4.0, 6.0])
    assert frame.wavelength_nm.tolist() == [400.0, 500.0, 600.0]
