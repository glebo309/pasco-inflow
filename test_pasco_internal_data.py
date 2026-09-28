import struct

import numpy as np

from pasco_internal_data import (
    classify_live_files,
    read_segment,
    read_with_rediscovery,
    write_live_csv,
)
from pasco_flow_data import read_pasco_spectrum


def write_segment(path, values):
    records = b"".join(struct.pack("<Id", 1, float(value)) for value in values)
    records += b"".join(struct.pack("<Id", 0, 0.0) for _ in values)
    path.write_bytes(records)


def test_read_segment_returns_only_valid_pasaco_values(tmp_path):
    path = tmp_path / "segment.tmp"
    write_segment(path, [379.8, 380.1, 380.3])

    assert read_segment(path).tolist() == [379.8, 380.1, 380.3]


def test_classify_live_files_finds_calibrated_wavelength_absorbance_and_transmittance(tmp_path):
    wavelength = np.array([379.8, 500.0, 950.0])
    absorbance = np.array([0.0, 0.3, 1.0])
    transmittance = 100.0 * np.power(10.0, -absorbance)
    wavelength_path = tmp_path / "wavelength.tmp"
    absorbance_path = tmp_path / "absorbance.tmp"
    transmittance_path = tmp_path / "transmittance.tmp"
    raw_path = tmp_path / "raw.tmp"
    write_segment(wavelength_path, wavelength)
    write_segment(absorbance_path, absorbance)
    write_segment(transmittance_path, transmittance)
    write_segment(raw_path, [1000, 2000, 3000])

    source = classify_live_files(
        [raw_path, transmittance_path, wavelength_path, absorbance_path]
    )

    assert source.wavelength_path == wavelength_path
    assert source.absorbance_path == absorbance_path
    assert source.transmittance_path == transmittance_path


def test_live_csv_is_readable_by_the_dashboard_parser(tmp_path):
    output = tmp_path / "inline_00001_t00002.000s.csv"
    write_live_csv(
        output,
        wavelength_nm=np.array([400.0, 500.0]),
        absorbance_au=np.array([0.1, 0.2]),
        transmittance_pct=np.array([79.43, 63.10]),
    )

    spectrum = read_pasco_spectrum(output)

    assert spectrum.wavelength_nm.tolist() == [400.0, 500.0]
    assert spectrum.absorbance_au.tolist() == [0.1, 0.2]
    assert spectrum.transmittance_pct.tolist() == [79.43, 63.1]


def test_live_csv_appears_atomically_under_its_final_name(tmp_path):
    output = tmp_path / "inline_00001_t00002.000s.csv"

    write_live_csv(
        output,
        wavelength_nm=np.array([400.0, 500.0]),
        absorbance_au=np.array([0.1, 0.2]),
        transmittance_pct=np.array([79.43, 63.10]),
    )

    # The dashboard polls this folder; nothing partial may match its glob
    # and no temporary file may be left behind.
    assert [path.name for path in tmp_path.iterdir()] == [output.name]


def test_live_reader_recovers_when_pasaco_rotates_its_temp_filenames():
    replacement = object()

    class RotatedSource:
        def read(self):
            raise FileNotFoundError("PASCO removed the first live filename")

    class CurrentSource:
        def read(self):
            return ("wavelength", "absorbance", "transmittance")

    snapshot, source = read_with_rediscovery(
        RotatedSource(),
        rediscover=lambda: (replacement, CurrentSource())[1],
        attempts=2,
        sleep=lambda *_: None,
    )

    assert snapshot == ("wavelength", "absorbance", "transmittance")
    assert isinstance(source, CurrentSource)


def test_live_data_age_reflects_pasco_recording_activity(tmp_path):
    import time as time_module

    from pasco_internal_data import live_data_age_s

    assert live_data_age_s(tmp_path) is None  # no live files: not recording

    live = tmp_path / "abc123.tmp"
    live.write_bytes(b"\x00" * 16)
    age = live_data_age_s(tmp_path)
    assert age is not None and age < 2.0  # fresh: Record is on

    old = time_module.time() - 60
    import os
    os.utime(live, (old, old))
    assert live_data_age_s(tmp_path) > 30  # stale: recording stopped
