from meridian.lib.state.timestamps import iso_timestamp_to_epoch


def test_iso_timestamp_to_epoch_normalizes_z_and_naive_utc() -> None:
    assert iso_timestamp_to_epoch("2024-01-01T00:00:00Z") == 1704067200.0
    assert iso_timestamp_to_epoch("2024-01-01T00:00:00") == 1704067200.0
    assert iso_timestamp_to_epoch("not-a-date") is None
    assert iso_timestamp_to_epoch(None) is None
