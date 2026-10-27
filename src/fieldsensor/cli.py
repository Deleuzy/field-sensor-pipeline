"""Command line entry point.

  fieldsensor simulate            write data/readings.csv and data/answer_key.csv
  fieldsensor ingest-csv          load data/readings.csv into raw_readings
  fieldsensor produce / consume   stream data/readings.csv through Redpanda into raw_readings
  fieldsensor validate            build validated_readings and quality_flags, score against the key
  fieldsensor run                 simulate + ingest-csv + validate in one go (no broker needed)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from . import simulate as sim
from . import store, stream
from .validate import score, validate

DATA = Path("data")


def cmd_simulate(args) -> None:
    DATA.mkdir(exist_ok=True)
    readings, key = sim.simulate(sim.SimConfig(days=args.days, seed=args.seed))
    readings.to_csv(DATA / "readings.csv", index=False)
    key.to_csv(DATA / "answer_key.csv", index=False)
    print(f"simulated {len(readings)} readings, {key.fault_id.nunique()} faults "
          f"({len(key)} affected readings) -> {DATA}/")


def _load_csv() -> pd.DataFrame:
    return pd.read_csv(DATA / "readings.csv", parse_dates=["ts"])


def _fresh_engine(reset: bool):
    engine = store.get_engine()
    store.init_db(engine, reset=reset)
    store.write_plots(engine, sim.plots_frame())
    return engine


def cmd_ingest_csv(args) -> None:
    engine = _fresh_engine(args.reset)
    n = store.append_raw(engine, _load_csv(), source="csv")
    print(f"ingested {n} rows from CSV")


def cmd_produce(args) -> None:
    n = stream.produce(_load_csv(), bootstrap=args.bootstrap)
    print(f"published {n} messages to {stream.TOPIC}")


def cmd_consume(args) -> None:
    engine = _fresh_engine(args.reset)
    print(json.dumps(stream.consume(engine, bootstrap=args.bootstrap, idle_seconds=args.idle)))


def cmd_validate(args) -> None:
    engine = store.get_engine()
    raw = store.read_raw(engine)
    validated, flags = validate(raw)
    store.write_validated(engine, validated, flags)
    print(f"validated {len(validated)} slots, {len(flags)} flags")
    key_path = DATA / "answer_key.csv"
    if key_path.exists():
        result = score(flags, pd.read_csv(key_path, parse_dates=["ts"]))
        (DATA / "score.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))


def cmd_run(args) -> None:
    cmd_simulate(args)
    args.reset = True
    cmd_ingest_csv(args)
    cmd_validate(args)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="fieldsensor")
    sub = p.add_subparsers(required=True)
    for name, fn in [("simulate", cmd_simulate), ("ingest-csv", cmd_ingest_csv), ("produce", cmd_produce),
                     ("consume", cmd_consume), ("validate", cmd_validate), ("run", cmd_run)]:
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        sp.add_argument("--days", type=int, default=21)
        sp.add_argument("--seed", type=int, default=42)
        sp.add_argument("--reset", action="store_true", help="drop and recreate tables first")
        sp.add_argument("--bootstrap", default="localhost:19092")
        sp.add_argument("--idle", type=float, default=5.0, help="consumer stops after this many idle seconds")
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
