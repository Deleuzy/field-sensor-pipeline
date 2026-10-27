"""Synthetic field-trial sensor data with injected faults and an answer key.

Everything here is simulated. Six plots in a randomised block layout, two
treatments (A = rain-fed control, B = drip-irrigated), four sensors per plot,
one reading every 10 minutes. Faults are injected on purpose and every faulty
reading is written to an answer key, so validation can be scored exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

STEP = pd.Timedelta(minutes=10)

# plot_id -> (treatment, block). Two blocks per treatment pair, randomised.
PLOTS: dict[str, tuple[str, int]] = {
    "P1": ("A", 1),
    "P2": ("B", 1),
    "P3": ("B", 2),
    "P4": ("A", 2),
    "P5": ("A", 3),
    "P6": ("B", 3),
}
SENSORS = ["air_temp_c", "rel_humidity_pct", "soil_moisture_vwc_pct", "co2_ppm"]
DECIMALS = {"air_temp_c": 2, "rel_humidity_pct": 2, "soil_moisture_vwc_pct": 2, "co2_ppm": 1}

FAULT_TYPES = ["missing", "stuck", "out_of_range", "spike", "duplicate", "clock_drift"]


@dataclass
class SimConfig:
    start: str = "2026-05-04 00:00"
    days: int = 21
    seed: int = 42
    # number of injected episodes per fault type
    n_faults: dict[str, int] = field(
        default_factory=lambda: {
            "missing": 4,
            "stuck": 3,
            "out_of_range": 4,
            "spike": 5,
            "duplicate": 3,
            "clock_drift": 1,
        }
    )


def _weather(rng: np.random.Generator, t: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    """Field-wide weather shared by all plots: temperature, humidity driver, rain."""
    n = len(t)
    hours = t.hour.to_numpy() + t.minute.to_numpy() / 60
    days = np.arange(n) / 144
    # Diurnal cycle peaking ~15:00, slow warming over spring, AR(1) weather noise.
    diurnal = np.sin(2 * np.pi * (hours - 9) / 24)
    synoptic = np.zeros(n)
    for i in range(1, n):
        synoptic[i] = 0.995 * synoptic[i - 1] + rng.normal(0, 0.08)
    temp = 10.5 + 0.12 * days + 5.5 * diurnal + synoptic
    # Rain: a handful of events, each lasting 1-4 hours.
    rain = np.zeros(n)
    for start in rng.choice(np.arange(144, n - 30), size=5, replace=False):
        dur = int(rng.integers(6, 25))
        rain[start : start + dur] = rng.uniform(0.3, 1.2)  # mm per 10 min
    return {"temp": temp, "diurnal": diurnal, "synoptic": synoptic, "rain": rain}


def _plot_series(rng, t, w, treatment: str) -> dict[str, np.ndarray]:
    n = len(t)
    hours = t.hour.to_numpy() + t.minute.to_numpy() / 60
    offset = rng.normal(0, 0.3)
    temp = w["temp"] + offset + rng.normal(0, 0.15, n)
    # Irrigated plots are slightly cooler by day (evaporative cooling).
    if treatment == "B":
        temp -= 0.4 * np.clip(w["diurnal"], 0, None)
    raining = w["rain"] > 0
    hum = 78 - 14 * w["diurnal"] - 1.5 * w["synoptic"] + 12 * raining + rng.normal(0, 0.8, n)
    # Soft cap near saturation, then sensor noise so saturated nights are not an exact flat line.
    hum = 97 - np.log1p(np.exp(97 - hum)) + rng.normal(0, 0.3, n)

    # Soil moisture: daytime evapotranspiration losses, rain gains, drip irrigation for B.
    soil = np.empty(n)
    soil[0] = rng.uniform(24, 27)
    et = 0.012 * np.clip(w["diurnal"], 0, None) + 0.002
    irrigation = np.zeros(n)
    if treatment == "B":
        # 2 h drip at 06:00 every third day
        morning = np.where((hours == 6.0))[0]
        for i in morning[::3]:
            irrigation[i : i + 12] = 0.35
    for i in range(1, n):
        gain = 0.9 * w["rain"][i] + irrigation[i]
        soil[i] = soil[i - 1] - et[i] * (soil[i - 1] / 25) + gain * (1 - soil[i - 1] / 45)
    soil = soil + rng.normal(0, 0.05, n)

    # Canopy CO2: night-time respiration build-up under a stable boundary layer,
    # daytime drawdown by photosynthesis. Wetter soil respires a little more.
    night = np.clip(-w["diurnal"], 0, None)
    co2 = 418 + 55 * night - 12 * np.clip(w["diurnal"], 0, None) + 0.8 * (soil - 25) + rng.normal(0, 4, n)

    return {
        "air_temp_c": temp,
        "rel_humidity_pct": hum,
        "soil_moisture_vwc_pct": soil,
        "co2_ppm": co2,
    }


def _clean(cfg: SimConfig, rng) -> pd.DataFrame:
    t = pd.date_range(cfg.start, periods=cfg.days * 144, freq=STEP)
    w = _weather(rng, t)
    frames = []
    for plot, (treatment, _block) in PLOTS.items():
        series = _plot_series(rng, t, w, treatment)
        for sensor, values in series.items():
            frames.append(
                pd.DataFrame(
                    {
                        "plot_id": plot,
                        "sensor": sensor,
                        "ts": t,
                        "value": np.round(values, DECIMALS[sensor]),
                    }
                )
            )
    return pd.concat(frames, ignore_index=True)


class _Placer:
    """Picks fault windows that never overlap each other on the same series."""

    MARGIN = 12  # slots kept clean around a fault so neighbouring rules stay independent

    def __init__(self, rng, n_slots: int):
        self.rng = rng
        self.n = n_slots
        self.used: dict[tuple[str, str], list[tuple[int, int]]] = {}

    def place(self, length: int, plot: str | None = None, sensor: str | None = None):
        for _ in range(1000):
            p = plot or self.rng.choice(list(PLOTS))
            s = sensor or self.rng.choice(SENSORS)
            start = int(self.rng.integers(self.MARGIN + 6, self.n - length - self.MARGIN - 6))
            lo, hi = start - self.MARGIN, start + length + self.MARGIN
            sensors = SENSORS if sensor == "*" else [s]
            if all(
                hi <= a or lo >= b for s2 in sensors for a, b in self.used.get((p, s2), [])
            ):
                for s2 in sensors:
                    self.used.setdefault((p, s2), []).append((lo, hi))
                return p, s, start
        raise RuntimeError("could not place fault")


def simulate(cfg: SimConfig | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (readings, answer_key).

    readings: plot_id, sensor, ts, value - in arrival order, as a logger would send them.
    answer_key: one row per affected reading: fault_id, fault_type, plot_id, sensor, ts.
    For 'missing' faults ts is the empty 10-minute slot.
    """
    cfg = cfg or SimConfig()
    rng = np.random.default_rng(cfg.seed)
    df = _clean(cfg, rng)
    t0 = pd.Timestamp(cfg.start)
    n_slots = cfg.days * 144
    df["slot"] = ((df["ts"] - t0) / STEP).astype(int)
    df["drop"] = False
    placer = _Placer(rng, n_slots)
    key = []
    extra_rows = []
    fid = 0

    def idx(p, s, a, b):
        return df.index[(df.plot_id == p) & (df.sensor == s) & (df.slot >= a) & (df.slot < b)]

    def record(ftype, p, s, timestamps):
        for ts in timestamps:
            key.append({"fault_id": fid, "fault_type": ftype, "plot_id": p, "sensor": s, "ts": ts})

    for ftype in FAULT_TYPES:
        for _ in range(cfg.n_faults.get(ftype, 0)):
            fid += 1
            if ftype == "missing":
                length = int(rng.integers(3, 37))  # 30 min to 6 h outage
                p, s, a = placer.place(length)
                rows = idx(p, s, a, a + length)
                df.loc[rows, "drop"] = True
                record(ftype, p, s, df.loc[rows, "ts"])
            elif ftype == "stuck":
                length = int(rng.integers(24, 73))  # 4 to 12 h flat line
                p, s, a = placer.place(length)
                rows = idx(p, s, a, a + length)
                df.loc[rows, "value"] = df.loc[rows[0], "value"]
                record(ftype, p, s, df.loc[rows, "ts"])
            elif ftype == "out_of_range":
                p, s, a = placer.place(1)
                row = idx(p, s, a, a + 1)
                df.loc[row, "value"] = rng.choice([-9999.0, 6553.5, {"air_temp_c": 85.0,
                    "rel_humidity_pct": 112.0, "soil_moisture_vwc_pct": -3.0, "co2_ppm": 120.0}[s]])
                record(ftype, p, s, df.loc[row, "ts"])
            elif ftype == "spike":
                p, s, a = placer.place(1)
                row = idx(p, s, a, a + 1)
                jump = {"air_temp_c": 9, "rel_humidity_pct": 25, "soil_moisture_vwc_pct": 9, "co2_ppm": 250}[s]
                sign = 1 if s in ("co2_ppm", "air_temp_c") else -1  # stay inside physical range
                df.loc[row, "value"] = df.loc[row, "value"] + sign * jump * rng.uniform(1.0, 1.5)
                df.loc[row, "value"] = df.loc[row, "value"].round(DECIMALS[s])
                record(ftype, p, s, df.loc[row, "ts"])
            elif ftype == "duplicate":
                p, s, a = placer.place(1)
                row = idx(p, s, a, a + 1)
                dup = df.loc[row].copy()
                extra_rows.append(dup)  # re-sent later, as a retransmission would be
                record(ftype, p, s, dup["ts"])
            elif ftype == "clock_drift":
                # A logger clock runs fast for ~3 h, then resyncs: all four sensors on the plot
                # get timestamps 15 s, 30 s, ... 240 s off the 10-minute grid.
                length = 16
                p, _s, a = placer.place(length, sensor="*")
                for s in SENSORS:
                    rows = idx(p, s, a, a + length)
                    drift = pd.to_timedelta(15 * np.arange(1, length + 1), unit="s")
                    df.loc[rows, "ts"] = df.loc[rows, "ts"].to_numpy() + drift.to_numpy()
                    record(ftype, p, s, df.loc[rows, "ts"])

    out = df[~df["drop"]]
    if extra_rows:
        out = pd.concat([out, *extra_rows], ignore_index=True)
    out = out[["plot_id", "sensor", "ts", "value"]].reset_index(drop=True)
    answer = pd.DataFrame(key, columns=["fault_id", "fault_type", "plot_id", "sensor", "ts"])
    return out, answer


def plots_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [{"plot_id": p, "treatment": t, "block": b} for p, (t, b) in PLOTS.items()]
    )
