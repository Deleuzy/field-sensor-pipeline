from fastapi.testclient import TestClient

from fieldsensor import api, store
from fieldsensor.validate import validate


def _load(engine, readings):
    store.append_raw(engine, readings, source="csv")
    validated, flags = validate(store.read_raw(engine))
    store.write_validated(engine, validated, flags)
    return validated, flags


def test_roundtrip_and_api(engine, simulated):
    readings, _ = simulated
    validated, flags = _load(engine, readings)
    raw = store.read_raw(engine)
    assert len(raw) == len(readings)

    api.app.dependency_overrides[api.get_engine] = lambda: engine
    client = TestClient(api.app)
    try:
        assert client.get("/health").json()["data"] == "simulated"
        assert len(client.get("/plots").json()) == 6

        latest = client.get("/plots/P2/latest").json()
        assert {r["sensor"] for r in latest["readings"]} == set(readings.sensor)
        assert client.get("/plots/NOPE/latest").status_code == 404

        summary = client.get("/quality/summary").json()
        assert summary["totals"]["slots"] == len(validated)
        assert summary["totals"]["flags"] == len(flags)
        assert summary["totals"]["missing"] == (validated.status == "missing").sum()

        stuck = client.get("/flags", params={"rule": "stuck", "limit": 5000}).json()
        assert len(stuck) == (flags.rule == "stuck").sum()
    finally:
        api.app.dependency_overrides.clear()
