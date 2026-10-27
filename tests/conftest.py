import pandas as pd
import pytest

from fieldsensor import simulate as sim
from fieldsensor import store


@pytest.fixture(scope="session")
def simulated():
    return sim.simulate(sim.SimConfig(days=14, seed=42))


@pytest.fixture
def engine(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/test.db"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = store.get_engine(url)
    store.init_db(eng, reset=True)
    store.write_plots(eng, sim.plots_frame())
    return eng


def as_raw(readings: pd.DataFrame) -> pd.DataFrame:
    return readings.reset_index(names="raw_id")
