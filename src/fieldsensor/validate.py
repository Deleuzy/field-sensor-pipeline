"""Explicit, rule-based quality checks for 10-minute sensor readings.

Nothing is dropped. Every raw reading stays in the raw table; validation writes
one row per expected 10-minute slot (inserting empty rows for gaps) and a flag
row for every rule a reading breaks. The rules are applied in a fixed order and
each later rule ignores readings an earlier rule already rejected, so one fault
produces one flag.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

STEP = pd.Timedelta(minutes=10)


@dataclass(frozen=True)
class Rule:
    name: str
    severity: str
    description: str


RULES = {
    r.name: r
    for r in [
        Rule("duplicate", "info", "Same plot, sensor and timestamp received more than once; first arrival kept."),
        Rule("off_grid", "warning", "Timestamp not on the 10-minute grid (logger clock drift); snapped to nearest slot."),
        Rule("out_of_range", "error", "Value outside the sensor's physical range."),
        Rule("missing", "warning", "No reading for an expected 10-minute slot; empty row inserted."),
        Rule("stuck", "error", "Identical value for 2 h or more (flat line)."),
        Rule("spike", "warning", "Value far from the centred 70-minute median of its neighbours."),
    ]
}

# Physical limits of the (simulated) sensors.
RANGES = {
    "air_temp_c": (-40.0, 60.0),
    "rel_humidity_pct": (0.0, 100.0),
    "soil_moisture_vwc_pct": (0.0, 60.0),
    "co2_ppm": (300.0, 5000.0),
}
# Largest plausible deviation from the local median.
SPIKE_LIMIT = {
    "air_temp_c": 4.0,
    "rel_humidity_pct": 15.0,
    "soil_moisture_vwc_pct": 4.0,
    "co2_ppm": 80.0,
}
STUCK_MIN_RUN = 12  # 12 x 10 min = 2 h
SPIKE_WINDOW = 7


def validate(raw: pd.DataFrame, start=None, end=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Validate raw readings.

    raw: columns raw_id, plot_id, sensor, ts, value (raw_id gives arrival order).
    start/end: first and last expected slot; default is the span of the data.
    Returns (validated, flags):
      validated - plot_id, sensor, slot_ts, raw_id, value, status ('ok'|'flagged'|'missing')
      flags     - raw_id, plot_id, sensor, ts, slot_ts, rule, severity, detail
    """
    raw = raw.copy()
    raw["ts"] = pd.to_datetime(raw["ts"])
    raw = raw.sort_values("raw_id")
    raw["slot_ts"] = raw["ts"].dt.round(STEP)
    start = pd.Timestamp(start) if start is not None else raw["slot_ts"].min()
    end = pd.Timestamp(end) if end is not None else raw["slot_ts"].max()
    grid = pd.date_range(start, end, freq=STEP)

    flags: list[dict] = []

    def flag(rows: pd.DataFrame, rule: str, detail):
        for r, d in zip(rows.itertuples(), detail if isinstance(detail, list) else [detail] * len(rows)):
            flags.append(
                {
                    "raw_id": getattr(r, "raw_id", None),
                    "plot_id": r.plot_id,
                    "sensor": r.sensor,
                    "ts": r.ts,
                    "slot_ts": r.slot_ts,
                    "rule": rule,
                    "severity": RULES[rule].severity,
                    "detail": d,
                }
            )

    # 1. duplicates: exact same raw timestamp re-sent; keep first arrival.
    dup = raw.duplicated(["plot_id", "sensor", "ts"], keep="first")
    flag(raw[dup], "duplicate", "re-sent reading; first arrival kept")
    primary = raw[~dup]

    # 2. off-grid timestamps (clock drift).
    off = primary["ts"] != primary["slot_ts"]
    offset_s = (primary.loc[off, "ts"] - primary.loc[off, "slot_ts"]).dt.total_seconds()
    flag(primary[off], "off_grid", [f"clock offset {s:+.0f} s" for s in offset_s])

    validated = []
    for (plot, sensor), g in primary.groupby(["plot_id", "sensor"], sort=True):
        g = g.drop_duplicates("slot_ts", keep="first").set_index("slot_ts").reindex(grid)
        g.index.name = "slot_ts"
        g["plot_id"], g["sensor"] = plot, sensor
        g = g.reset_index()
        status = pd.Series("ok", index=g.index)
        usable = g["value"].notna()

        # 3. out of range
        lo, hi = RANGES[sensor]
        bad = usable & ((g["value"] < lo) | (g["value"] > hi))
        flag(g[bad], "out_of_range", [f"{v} outside [{lo}, {hi}]" for v in g.loc[bad, "value"]])
        status[bad] = "flagged"
        usable &= ~bad

        # 4. missing slots: inserted as visible empty rows
        miss = g["value"].isna()
        flag(g[miss].assign(ts=g.loc[miss, "slot_ts"]), "missing", "no reading received")
        status[miss] = "missing"

        # 5. stuck: run of identical values
        v = g["value"].where(usable)
        run_id = (v != v.shift()).cumsum()
        run_len = v.groupby(run_id).transform("size")
        stuck = usable & (run_len >= STUCK_MIN_RUN)
        flag(g[stuck], "stuck", [f"flat at {x} for {n * 10} min" for x, n in zip(g.loc[stuck, "value"], run_len[stuck])])
        status[stuck] = "flagged"
        usable &= ~stuck

        # 6. spikes against a centred rolling median of usable neighbours
        v = g["value"].where(usable)
        med = v.rolling(SPIKE_WINDOW, center=True, min_periods=3).median()
        dev = (v - med).abs()
        spike = usable & (dev > SPIKE_LIMIT[sensor])
        flag(g[spike], "spike", [f"{d:.1f} from local median {m:.1f}" for d, m in zip(dev[spike], med[spike])])
        status[spike] = "flagged"

        g["status"] = status
        # off-grid readings stay in the series but are marked
        g.loc[g["ts"].notna() & (g["ts"] != g["slot_ts"]) & (g["status"] == "ok"), "status"] = "flagged"
        validated.append(g[["plot_id", "sensor", "slot_ts", "raw_id", "value", "status"]])

    validated = pd.concat(validated, ignore_index=True)
    validated["raw_id"] = validated["raw_id"].astype("Int64")
    flags = pd.DataFrame(
        flags, columns=["raw_id", "plot_id", "sensor", "ts", "slot_ts", "rule", "severity", "detail"]
    )
    flags["raw_id"] = flags["raw_id"].astype("Int64")
    return validated, flags


# Which validation rule is expected to catch each injected fault type.
EXPECTED_RULE = {
    "missing": "missing",
    "stuck": "stuck",
    "out_of_range": "out_of_range",
    "spike": "spike",
    "duplicate": "duplicate",
    "clock_drift": "off_grid",
}


def score(flags: pd.DataFrame, answer_key: pd.DataFrame) -> dict:
    """Compare flags with the simulator's answer key.

    A fault counts as caught when every reading it affected carries the expected
    flag. Any flag not explained by the answer key is a false positive.
    """
    key = answer_key.copy()
    key["ts"] = pd.to_datetime(key["ts"])
    key["rule"] = key["fault_type"].map(EXPECTED_RULE)
    expected = set(zip(key.plot_id, key.sensor, key.ts, key.rule))
    got = set(zip(flags.plot_id, flags.sensor, pd.to_datetime(flags.ts), flags.rule))
    key["hit"] = [t in got for t in zip(key.plot_id, key.sensor, key.ts, key.rule)]
    per_fault = key.groupby(["fault_id", "fault_type"])["hit"].all().reset_index()
    by_type = {}
    for ftype, g in per_fault.groupby("fault_type"):
        readings = key[key.fault_type == ftype]
        by_type[ftype] = {
            "faults_injected": int(len(g)),
            "faults_caught": int(g["hit"].sum()),
            "readings_affected": int(len(readings)),
            "readings_flagged": int(readings["hit"].sum()),
        }
    false_pos = sorted(got - expected, key=str)
    return {
        "faults_injected": int(len(per_fault)),
        "faults_caught": int(per_fault["hit"].sum()),
        "readings_affected": int(len(key)),
        "readings_flagged_correctly": int(key["hit"].sum()),
        "false_positive_flags": len(false_pos),
        "false_positive_examples": [list(map(str, f)) for f in false_pos[:10]],
        "by_type": by_type,
    }
