import pandas as pd
import pytest

from fieldsensor import simulate as sim
from fieldsensor.validate import RULES, score, validate

from .conftest import as_raw


@pytest.mark.parametrize("seed", [42, 1, 2, 3, 4, 5])
def test_every_injected_fault_caught_and_nothing_else(seed):
    readings, key = sim.simulate(sim.SimConfig(days=14, seed=seed))
    _, flags = validate(as_raw(readings))
    result = score(flags, key)
    assert result["faults_caught"] == result["faults_injected"] == key.fault_id.nunique()
    assert result["readings_flagged_correctly"] == result["readings_affected"]
    assert result["false_positive_flags"] == 0, result["false_positive_examples"]


def test_no_flags_on_clean_data():
    cfg = sim.SimConfig(days=7, seed=3, n_faults={})
    readings, key = sim.simulate(cfg)
    assert key.empty
    validated, flags = validate(as_raw(readings))
    assert flags.empty
    assert (validated.status == "ok").all()


def test_nothing_dropped_and_gaps_visible(simulated):
    readings, key = simulated
    raw = as_raw(readings)
    validated, flags = validate(raw)
    # one row per plot x sensor x slot, including the gaps
    assert len(validated) == 14 * 144 * len(sim.PLOTS) * len(sim.SENSORS)
    missing = validated[validated.status == "missing"]
    assert len(missing) == (key.fault_type == "missing").sum()
    assert missing.value.isna().all()
    # every raw reading is either in the validated table or flagged as a duplicate
    in_validated = set(validated.raw_id.dropna().astype(int))
    dup_ids = set(flags[flags.rule == "duplicate"].raw_id.astype(int))
    assert in_validated | dup_ids == set(raw.raw_id)
    # flagged values are kept as received
    oor = flags[flags.rule == "out_of_range"]
    kept = validated.set_index("raw_id").loc[oor.raw_id.astype(int)]
    assert (kept.status == "flagged").all() and kept.value.notna().all()


def test_flags_carry_plot_sensor_rule_severity(simulated):
    readings, _ = simulated
    _, flags = validate(as_raw(readings))
    assert flags[["plot_id", "sensor", "rule", "severity", "ts"]].notna().all().all()
    for rule, sev in flags[["rule", "severity"]].drop_duplicates().itertuples(index=False):
        assert RULES[rule].severity == sev


def _series(values, start="2026-05-01"):
    ts = pd.date_range(start, periods=len(values), freq="10min")
    return pd.DataFrame({"raw_id": range(len(values)), "plot_id": "P1",
                         "sensor": "air_temp_c", "ts": ts, "value": values})


def test_rules_on_small_handmade_series():
    base = [10 + 0.1 * (i % 5) + 0.01 * i for i in range(60)]
    vals = list(base)
    vals[10] = 99.0               # out of range
    vals[20] = vals[20] + 8       # spike
    for i in range(30, 44):       # stuck for 14 slots
        vals[i] = 11.11
    raw = _series(vals)
    raw = raw.drop(index=[50, 51])                        # gap of two slots
    raw = pd.concat([raw, raw[raw.raw_id == 5].assign(raw_id=999)])  # re-sent reading
    raw.loc[raw.raw_id == 55, "ts"] += pd.Timedelta(seconds=90)      # clock offset
    validated, flags = validate(raw)
    got = flags.groupby("rule").size().to_dict()
    assert got == {"out_of_range": 1, "spike": 1, "stuck": 14, "missing": 2, "duplicate": 1, "off_grid": 1}
    assert len(validated) == 60
