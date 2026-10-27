"""The streaming path, tested without a broker: same messages, same result as CSV."""

import pandas as pd

from fieldsensor import store, stream
from fieldsensor.validate import validate


def test_message_roundtrip():
    row = {"plot_id": "P3", "sensor": "co2_ppm", "ts": pd.Timestamp("2026-05-04 10:00:15"), "value": 421.5}
    key, value = stream.to_message(row)
    assert key == b"P3"
    assert stream.from_message(value) == row


def test_stream_sink_matches_csv_ingest(engine, simulated, tmp_path):
    readings, _ = simulated
    messages = [stream.to_message(r)[1] for r in readings.to_dict("records")]
    messages.insert(100, b"not json")
    messages.insert(200, b'{"plot_id": "P1"}')
    result = stream.sink(engine, messages, batch_size=7000)
    assert result == {"written": len(readings), "rejected": 2}

    via_stream = validate(store.read_raw(engine))[1]
    via_csv = validate(readings.reset_index(names="raw_id"))[1]
    cols = ["plot_id", "sensor", "ts", "rule"]
    pd.testing.assert_frame_equal(
        via_stream[cols].sort_values(cols).reset_index(drop=True),
        via_csv[cols].sort_values(cols).reset_index(drop=True),
    )
