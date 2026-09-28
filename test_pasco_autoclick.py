import pytest

from pasco_autoclick import click_deadlines, run_measurements, validate_run_settings


def test_click_deadlines_do_not_accumulate_drift():
    assert click_deadlines(start=100.0, interval_s=2.0, count=4) == [
        100.0,
        102.0,
        104.0,
        106.0,
    ]


def test_validate_run_settings_converts_minutes_to_spectrum_count():
    interval_s, scan_time_s, count = validate_run_settings(
        interval_s=2.0,
        scan_time_s=0.5,
        minutes=10.0,
    )

    assert interval_s == 2.0
    assert scan_time_s == 0.5
    assert count == 300


def test_validate_run_settings_supports_continuous_capture():
    interval_s, scan_time_s, count = validate_run_settings(
        interval_s=2.0,
        scan_time_s=0.5,
        minutes=None,
        continuous=True,
    )

    assert interval_s == 2.0
    assert scan_time_s == 0.5
    assert count is None


@pytest.mark.parametrize(
    ("interval_s", "scan_time_s", "minutes"),
    [(0, 0.5, 10), (-1, 0.5, 10), (2, 0, 10), (2, 2, 10), (2, 0.5, 0), (2, 0.5, -1)],
)
def test_validate_run_settings_rejects_invalid_values(interval_s, scan_time_s, minutes):
    with pytest.raises(ValueError):
        validate_run_settings(
            interval_s=interval_s,
            scan_time_s=scan_time_s,
            minutes=minutes,
        )


def test_measurement_pairs_start_and_stop_each_spectrum_on_schedule():
    now = [100.0]
    clicks = []

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    def click():
        clicks.append(now[0])

    run_measurements(
        interval_s=2.0,
        scan_time_s=0.5,
        count=3,
        click=click,
        clock=clock,
        sleep=sleep,
        report=lambda *_: None,
    )

    assert clicks == [100.0, 100.5, 102.0, 102.5, 104.0, 104.5]
