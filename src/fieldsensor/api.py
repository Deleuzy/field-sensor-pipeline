"""Read-only HTTP API over the validated data.

  GET /health
  GET /plots                      plots with treatment and block
  GET /plots/{plot_id}/latest     latest slot per sensor, with status and any flags
  GET /quality/summary            flag counts and completeness per plot and sensor
  GET /flags?plot_id=&rule=&limit=

Run: uvicorn fieldsensor.api:app
"""

from __future__ import annotations

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import store

app = FastAPI(title="Field sensor pipeline (simulated data)", version="0.1.0")


def get_engine() -> Engine:
    return store.get_engine()


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.astype(object).where(df.notna(), None)
    return df.to_dict("records")


@app.get("/health")
def health():
    return {"status": "ok", "data": "simulated"}


@app.get("/plots")
def plots(engine: Engine = Depends(get_engine)):
    return _records(pd.read_sql("SELECT * FROM plots ORDER BY plot_id", engine))


@app.get("/plots/{plot_id}/latest")
def latest(plot_id: str, engine: Engine = Depends(get_engine)):
    """Most recent 10-minute slot for each sensor. A missing or flagged reading is
    returned as such, not replaced by an older good one."""
    q = text(
        """
        SELECT v.plot_id, v.sensor, v.slot_ts, v.value, v.status
        FROM validated_readings v
        WHERE v.plot_id = :p
          AND v.slot_ts = (SELECT MAX(slot_ts) FROM validated_readings WHERE plot_id = :p)
        ORDER BY v.sensor
        """
    )
    rows = pd.read_sql(q, engine, params={"p": plot_id})
    if rows.empty:
        raise HTTPException(404, f"no data for plot {plot_id}")
    flags = pd.read_sql(
        text("SELECT sensor, slot_ts, rule, severity, detail FROM quality_flags WHERE plot_id = :p"),
        engine, params={"p": plot_id},
    )
    out = []
    for r in _records(rows):
        f = flags[(flags.sensor == r["sensor"]) & (flags.slot_ts.astype(str) == str(r["slot_ts"]))]
        r["flags"] = _records(f[["rule", "severity", "detail"]])
        out.append(r)
    return {"plot_id": plot_id, "readings": out}


@app.get("/quality/summary")
def quality_summary(engine: Engine = Depends(get_engine)):
    counts = pd.read_sql(
        "SELECT plot_id, sensor, rule, severity, COUNT(*) AS n FROM quality_flags "
        "GROUP BY plot_id, sensor, rule, severity ORDER BY plot_id, sensor, rule", engine
    )
    completeness = pd.read_sql(
        "SELECT plot_id, sensor, COUNT(*) AS slots, "
        "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok, "
        "SUM(CASE WHEN status = 'flagged' THEN 1 ELSE 0 END) AS flagged, "
        "SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing "
        "FROM validated_readings GROUP BY plot_id, sensor ORDER BY plot_id, sensor", engine
    )
    completeness["pct_ok"] = (100 * completeness.ok / completeness.slots).round(2)
    by_rule = counts.groupby(["rule", "severity"], as_index=False)["n"].sum()
    return {
        "totals": {
            "slots": int(completeness.slots.sum()),
            "ok": int(completeness.ok.sum()),
            "flagged": int(completeness.flagged.sum()),
            "missing": int(completeness.missing.sum()),
            "flags": int(counts.n.sum()) if len(counts) else 0,
        },
        "by_rule": _records(by_rule),
        "by_plot_sensor": _records(completeness),
    }


@app.get("/flags")
def flags(plot_id: str | None = None, rule: str | None = None,
          limit: int = Query(200, le=5000), engine: Engine = Depends(get_engine)):
    sql = "SELECT * FROM quality_flags WHERE 1=1"
    params = {}
    if plot_id:
        sql += " AND plot_id = :p"
        params["p"] = plot_id
    if rule:
        sql += " AND rule = :r"
        params["r"] = rule
    sql += " ORDER BY ts LIMIT :lim"
    params["lim"] = limit
    return _records(pd.read_sql(text(sql), engine, params=params))
