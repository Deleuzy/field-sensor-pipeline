# field-sensor-pipeline

A small pipeline that collects, checks, stores and shows sensor data from agricultural field trials:
simulator → CSV or Redpanda stream → rule-based validation → SQLite/PostgreSQL → FastAPI + Streamlit.

> **All data in this repository is simulated.** No real field trial, farm or sensor is behind it.
> The numbers below describe how the pipeline handles the simulated data, not any real crop or site.
> This is a 2026 portfolio project, built to show the approach.

![Dashboard on simulated data](docs/dashboard.jpg)

## What it does

**Simulator** (`src/fieldsensor/simulate.py`). Six plots in three blocks, two treatments
(A = rain-fed control, B = drip-irrigated every third morning). Each plot has four sensors: air
temperature, relative humidity, soil moisture (volumetric water content) and canopy CO2, read every
10 minutes for 21 days. The signals have daily cycles (temperature peaks mid-afternoon, humidity is
highest before dawn, CO2 builds up at night from respiration), shared weather and rain events, and
plot-level offsets and noise. It then injects faults on purpose and writes every affected reading to
an answer key (`data/answer_key.csv`):

| Fault | How it is injected |
|---|---|
| missing | 30 min to 6 h of readings never arrive |
| stuck | sensor repeats one value for 4 to 12 h |
| out_of_range | logger error codes (-9999, 6553.5) or impossible values |
| spike | one reading jumps far from its neighbours, inside the physical range |
| duplicate | a reading is re-sent later with the same timestamp |
| clock_drift | a plot logger's clock runs fast for ~3 h (15 s to 240 s off the grid), then resyncs |

**Ingestion.** Batch: `fieldsensor ingest-csv`. Streaming: `fieldsensor produce` publishes each
reading as JSON to a Redpanda (Kafka API) topic keyed by plot, and `fieldsensor consume` reads it in
batches into the same raw table, counting malformed messages instead of storing them.

**Validation** (`src/fieldsensor/validate.py`). Explicit rules, applied in a fixed order. A later
rule skips readings an earlier rule has already rejected, so one fault gives one flag.

| Rule | Severity | Check |
|---|---|---|
| duplicate | info | same plot, sensor and timestamp seen again; first arrival kept |
| off_grid | warning | timestamp not on the 10-minute grid; snapped to the nearest slot |
| out_of_range | error | outside the sensor's physical range |
| missing | warning | expected slot with no reading; an empty row is inserted |
| stuck | error | identical value for 12 slots (2 h) or more |
| spike | warning | more than a per-sensor limit away from the centred 7-slot median |

Nothing is dropped. Raw readings stay in `raw_readings` as received. `validated_readings` has
exactly one row per plot, sensor and 10-minute slot, with status `ok`, `flagged` or `missing`, and
the flagged value is kept. `quality_flags` has one row per broken rule with plot, sensor, timestamp,
rule, severity and a detail text.

**Storage.** SQLite by default (`data/field.db`). Set `DATABASE_URL` to use PostgreSQL; a Postgres
service is in `docker-compose.yml` behind the `postgres` profile.

**API** (`src/fieldsensor/api.py`): `/health`, `/plots`, `/plots/{plot_id}/latest` (latest slot per
sensor, with its status and flags, never silently replaced by an older good value),
`/quality/summary` (completeness and flag counts per plot and sensor), `/flags?plot_id=&rule=`.

**Dashboard** (`dashboard/app.py`, Streamlit): daily treatment means per sensor (flagged readings
excluded by default, with a toggle), one plot's full series with flagged points and gaps marked,
and the flag table.

## Results from one run

`fieldsensor run` with the defaults (seed 42, 21 days, 6 plots x 4 sensors = 72,576 expected slots):

| Fault type | Injected | Caught | Readings affected | Readings flagged with the expected rule |
|---|---|---|---|---|
| missing | 4 | 4 | 97 | 97 |
| stuck | 3 | 3 | 123 | 123 |
| out_of_range | 4 | 4 | 4 | 4 |
| spike | 5 | 5 | 5 | 5 |
| duplicate | 3 | 3 | 3 | 3 |
| clock_drift | 1 | 1 | 64 (16 slots x 4 sensors) | 64 |
| **total** | **20** | **20** | **296** | **296** |

72,482 readings received, 296 flags, **0 flags outside the answer key**. The same result came from
the CSV path into SQLite and from the Redpanda stream path into PostgreSQL. The full output is
written to `data/score.json`.

How "caught" is scored: a fault counts as caught only if every reading it affected carries the
expected rule, and any flag not in the answer key counts as a false positive.

A check worth reporting: on the first version, a sweep over 40 seeds found false "stuck" flags.
Simulated humidity hit a hard cap at night and became an exact flat line. Real humidity sensors also
saturate near 100 % in dew, so the fix was to model sensor noise after the cap, and the limitation is
noted below. After the fix, all 40 seeds scored 20/20 with zero false positives. The tests check six
of them.

## How to run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,stream,dashboard]"

fieldsensor run                      # simulate, ingest CSV, validate, score (no Docker)
pytest -q                            # offline tests
uvicorn fieldsensor.api:app          # http://localhost:8000/docs
streamlit run dashboard/app.py
```

Streaming path (needs Docker; Redpanda is capped at 512 MB):

```bash
fieldsensor simulate
docker compose up -d --wait redpanda
fieldsensor produce
fieldsensor consume --reset          # stops after 5 s with no new messages
fieldsensor validate
docker compose down -v
```

With PostgreSQL instead of SQLite (also `pip install -e ".[postgres]"`):

```bash
docker compose --profile postgres up -d --wait
export DATABASE_URL=postgresql+psycopg://field:field-local-only@localhost:55432/field
fieldsensor consume --reset && fieldsensor validate
docker compose --profile postgres down -v
```

CI (GitHub Actions) installs the package, runs `pytest` and a 7-day end-to-end run on Python 3.11
and 3.12. It needs no Docker: the stream tests feed messages straight into the consumer's sink.

## Tests

17 tests, all offline: simulator determinism, plausible ranges, daily cycle and treatment effect;
every injected fault caught and nothing else for six seeds; zero flags on fault-free data; each rule
on a small hand-made series; nothing dropped and gaps visible; storage round trip and API endpoints;
stream message format, malformed messages, and stream ingest giving the same flags as CSV ingest.

## Limitations

- The data is simulated and the thresholds were tuned with knowledge of that simulator. A perfect
  score here says the rules match the faults they were built for, not that they would score the same
  on real field data. Real data would need thresholds set per sensor model and season.
- Real humidity sensors can read a flat 100 % for hours in dew or fog; the stuck rule would flag that.
  A real deployment would need a saturation exception.
- Only single-reading spikes are injected. A sustained level shift (for example after recalibration)
  is not detected; the centred median is built to tolerate step changes such as irrigation.
- Off-grid timestamps are snapped to the nearest slot, which only works for drift under 5 minutes.
- Validation recomputes the whole dataset in batch; it is not incremental per message.
- The consumer stops after an idle timeout; it is a demo, not a long-running service with retries,
  a dead-letter topic or schema registry.
- No authentication on the API, no deployment manifests, no MLOps (no models are trained here).

## Licence

MIT, see [LICENSE](LICENSE).
