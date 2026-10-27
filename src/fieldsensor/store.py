"""Storage: SQLite by default, PostgreSQL when DATABASE_URL points at one.

Tables
  plots              plot_id, treatment, block
  raw_readings       everything received, as received (raw_id = arrival order)
  validated_readings one row per plot x sensor x 10-minute slot, gaps included
  quality_flags      one row per rule broken
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

DEFAULT_URL = "sqlite:///data/field.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS plots (
    plot_id   TEXT PRIMARY KEY,
    treatment TEXT NOT NULL,
    block     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_readings (
    raw_id      INTEGER PRIMARY KEY,
    plot_id     TEXT NOT NULL,
    sensor      TEXT NOT NULL,
    ts          TIMESTAMP NOT NULL,
    value       DOUBLE PRECISION,
    source      TEXT NOT NULL,
    ingested_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS validated_readings (
    plot_id TEXT NOT NULL,
    sensor  TEXT NOT NULL,
    slot_ts TIMESTAMP NOT NULL,
    raw_id  INTEGER,
    value   DOUBLE PRECISION,
    status  TEXT NOT NULL,
    PRIMARY KEY (plot_id, sensor, slot_ts)
);
CREATE TABLE IF NOT EXISTS quality_flags (
    raw_id   INTEGER,
    plot_id  TEXT NOT NULL,
    sensor   TEXT NOT NULL,
    ts       TIMESTAMP NOT NULL,
    slot_ts  TIMESTAMP NOT NULL,
    rule     TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS ix_validated_plot ON validated_readings (plot_id, slot_ts);
CREATE INDEX IF NOT EXISTS ix_flags_plot ON quality_flags (plot_id, sensor);
"""


def get_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        os.makedirs(os.path.dirname(url.removeprefix("sqlite:///")) or ".", exist_ok=True)
    return create_engine(url)


def init_db(engine: Engine, reset: bool = False) -> None:
    with engine.begin() as con:
        if reset:
            for t in ("quality_flags", "validated_readings", "raw_readings", "plots"):
                con.execute(text(f"DROP TABLE IF EXISTS {t}"))
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                con.execute(text(stmt))


def write_plots(engine: Engine, plots: pd.DataFrame) -> None:
    with engine.begin() as con:
        con.execute(text("DELETE FROM plots"))
    plots.to_sql("plots", engine, if_exists="append", index=False)


def append_raw(engine: Engine, readings: pd.DataFrame, source: str) -> int:
    """Append readings (plot_id, sensor, ts, value) in the given order. Returns rows written."""
    with engine.connect() as con:
        next_id = con.execute(text("SELECT COALESCE(MAX(raw_id), 0) FROM raw_readings")).scalar() + 1
    df = readings[["plot_id", "sensor", "ts", "value"]].copy()
    df["ts"] = pd.to_datetime(df["ts"])
    df.insert(0, "raw_id", range(next_id, next_id + len(df)))
    df["source"] = source
    df["ingested_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    df.to_sql("raw_readings", engine, if_exists="append", index=False, chunksize=5000)
    return len(df)


def read_raw(engine: Engine) -> pd.DataFrame:
    return pd.read_sql("SELECT raw_id, plot_id, sensor, ts, value FROM raw_readings ORDER BY raw_id", engine, parse_dates=["ts"])


def write_validated(engine: Engine, validated: pd.DataFrame, flags: pd.DataFrame) -> None:
    with engine.begin() as con:
        con.execute(text("DELETE FROM validated_readings"))
        con.execute(text("DELETE FROM quality_flags"))
    validated.to_sql("validated_readings", engine, if_exists="append", index=False, chunksize=5000)
    flags.to_sql("quality_flags", engine, if_exists="append", index=False, chunksize=5000)
