"""Streaming path: publish readings to a Kafka-API broker (Redpanda) and consume them.

The broker client (kafka-python) is only imported inside produce()/consume(), so the
message format and the batching consumer logic can be tested offline.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator

import pandas as pd
from sqlalchemy.engine import Engine

from . import store

TOPIC = "field-readings"


def to_message(row) -> tuple[bytes, bytes]:
    """(key, value) for one reading. Keyed by plot so a plot's readings stay in order."""
    payload = {
        "plot_id": row["plot_id"],
        "sensor": row["sensor"],
        "ts": pd.Timestamp(row["ts"]).isoformat(),
        "value": None if pd.isna(row["value"]) else float(row["value"]),
    }
    return row["plot_id"].encode(), json.dumps(payload).encode()


def from_message(value: bytes) -> dict:
    msg = json.loads(value)
    missing = {"plot_id", "sensor", "ts", "value"} - msg.keys()
    if missing:
        raise ValueError(f"message missing fields: {sorted(missing)}")
    msg["ts"] = pd.Timestamp(msg["ts"])
    return msg


def sink(engine: Engine, values: Iterable[bytes], batch_size: int = 2000) -> dict:
    """Write message values to raw_readings in batches. Malformed messages are counted, not stored."""
    batch, written, rejected = [], 0, 0
    for v in values:
        try:
            batch.append(from_message(v))
        except (ValueError, json.JSONDecodeError):
            rejected += 1
            continue
        if len(batch) >= batch_size:
            written += store.append_raw(engine, pd.DataFrame(batch), source="stream")
            batch = []
    if batch:
        written += store.append_raw(engine, pd.DataFrame(batch), source="stream")
    return {"written": written, "rejected": rejected}


def produce(readings: pd.DataFrame, bootstrap: str = "localhost:19092", topic: str = TOPIC) -> int:
    from kafka import KafkaProducer

    producer = KafkaProducer(bootstrap_servers=bootstrap, linger_ms=20, acks="all")
    for row in readings.to_dict("records"):
        key, value = to_message(row)
        producer.send(topic, key=key, value=value)
    producer.flush()
    producer.close()
    return len(readings)


def _poll(consumer, idle_seconds: float) -> Iterator[bytes]:
    last = time.monotonic()
    while time.monotonic() - last < idle_seconds:
        records = consumer.poll(timeout_ms=500, max_records=5000)
        for batch in records.values():
            for rec in batch:
                yield rec.value
            last = time.monotonic()


def consume(engine: Engine, bootstrap: str = "localhost:19092", topic: str = TOPIC,
            idle_seconds: float = 5.0, group: str = "field-ingest") -> dict:
    """Consume until the topic has been idle for idle_seconds, then commit offsets."""
    from kafka import KafkaConsumer

    consumer = KafkaConsumer(topic, bootstrap_servers=bootstrap, group_id=group,
                             auto_offset_reset="earliest", enable_auto_commit=False)
    try:
        result = sink(engine, _poll(consumer, idle_seconds))
        consumer.commit()
    finally:
        consumer.close()
    return result
