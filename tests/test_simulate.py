import pandas as pd

from fieldsensor import simulate as sim


def test_deterministic_for_seed():
    a, ka = sim.simulate(sim.SimConfig(days=3, seed=7))
    b, kb = sim.simulate(sim.SimConfig(days=3, seed=7))
    pd.testing.assert_frame_equal(a, b)
    pd.testing.assert_frame_equal(ka, kb)


def test_shape_and_answer_key(simulated):
    readings, key = simulated
    slots = 14 * 144
    expected_rows = slots * len(sim.PLOTS) * len(sim.SENSORS)
    n_missing = (key.fault_type == "missing").sum()
    n_dup = (key.fault_type == "duplicate").sum()
    assert len(readings) == expected_rows - n_missing + n_dup
    assert set(key.fault_type) == set(sim.FAULT_TYPES)


def test_clean_readings_are_plausible(simulated):
    readings, key = simulated
    k = key.assign(ts=pd.to_datetime(key.ts))
    faulty = set(zip(k.plot_id, k.sensor, k.ts))
    clean = readings[[t not in faulty for t in zip(readings.plot_id, readings.sensor, readings.ts)]]
    bounds = {"air_temp_c": (-5, 30), "rel_humidity_pct": (30, 100),
              "soil_moisture_vwc_pct": (10, 45), "co2_ppm": (380, 520)}
    for sensor, (lo, hi) in bounds.items():
        v = clean[clean.sensor == sensor].value
        assert v.between(lo, hi).all(), sensor


def test_daily_cycle_and_treatment_effect(simulated):
    readings, _ = simulated
    r = readings[readings.value.between(-100, 1000)]
    temp = r[r.sensor == "air_temp_c"]
    by_hour = temp.groupby(temp.ts.dt.hour).value.mean()
    assert by_hour.idxmax() in range(13, 18) and by_hour.idxmin() in range(2, 8)
    soil = r[r.sensor == "soil_moisture_vwc_pct"].copy()
    soil["treatment"] = soil.plot_id.map(lambda p: sim.PLOTS[p][0])
    means = soil.groupby("treatment").value.mean()
    assert means["B"] > means["A"]  # irrigated plots are wetter
