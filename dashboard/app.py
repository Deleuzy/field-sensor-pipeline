"""Streamlit dashboard: compare treatments, with quality flags kept in view.

Run: streamlit run dashboard/app.py
"""

import altair as alt
import pandas as pd
import streamlit as st

from fieldsensor import store

st.set_page_config(page_title="Field trial sensors (simulated)", layout="wide")
st.title("Field trial sensors")
st.warning("All data on this page is simulated. No real field trial or sensor is behind it.")

engine = store.get_engine()


@st.cache_data
def load():
    v = pd.read_sql(
        "SELECT v.*, p.treatment, p.block FROM validated_readings v JOIN plots p USING (plot_id)",
        engine, parse_dates=["slot_ts"],
    )
    f = pd.read_sql("SELECT * FROM quality_flags", engine, parse_dates=["ts", "slot_ts"])
    return v, f


v, f = load()
sensors = sorted(v.sensor.unique())
sensor = st.sidebar.selectbox("Sensor", sensors, index=sensors.index("soil_moisture_vwc_pct"))
include_flagged = st.sidebar.checkbox("Include flagged readings in treatment means", value=False)

c1, c2, c3, c4 = st.columns(4)
c1.metric("10-min slots", f"{len(v):,}")
c2.metric("OK", f"{(v.status == 'ok').mean():.2%}")
c3.metric("Flagged", f"{(v.status == 'flagged').sum():,}")
c4.metric("Missing (empty rows)", f"{(v.status == 'missing').sum():,}")

st.subheader(f"Daily mean by treatment: {sensor}")
s = v[v.sensor == sensor]
use = s[s.status == "ok"] if not include_flagged else s[s.status != "missing"]
daily = use.groupby([use.slot_ts.dt.date.rename("day"), "treatment"]).value.agg(["mean", "count"]).reset_index()
st.altair_chart(
    alt.Chart(daily).mark_line(point=True).encode(
        x="day:T", y=alt.Y("mean:Q", title=sensor, scale=alt.Scale(zero=False)),
        color=alt.Color("treatment:N", title="Treatment (A rain-fed, B irrigated)"),
        tooltip=["day:T", "treatment", alt.Tooltip("mean:Q", format=".2f"), "count"],
    ),
    use_container_width=True,
)

st.subheader("One plot, every slot, flags marked")
plot = st.selectbox("Plot", sorted(v.plot_id.unique()))
p = s[s.plot_id == plot]
pf = f[(f.plot_id == plot) & (f.sensor == sensor)]
base = alt.Chart(p[p.status != "missing"]).encode(x="slot_ts:T")
line = base.mark_line(opacity=0.7).encode(y=alt.Y("value:Q", scale=alt.Scale(zero=False)))
pts = (
    alt.Chart(p[p.status == "flagged"].merge(pf[["slot_ts", "rule"]], on="slot_ts", how="left"))
    .mark_point(color="red", size=60, filled=True)
    .encode(x="slot_ts:T", y="value:Q", tooltip=["slot_ts:T", "value", "rule"])
)
gaps = alt.Chart(p[p.status == "missing"]).mark_rule(color="orange", opacity=0.4).encode(x="slot_ts:T")
st.altair_chart(line + pts + gaps, use_container_width=True)
st.caption("Red points: flagged readings (kept, not dropped). Orange bands: missing slots (inserted empty rows).")

st.subheader("Quality flags")
summary = f.groupby(["plot_id", "sensor", "rule", "severity"]).size().rename("n").reset_index()
st.dataframe(summary, use_container_width=True, hide_index=True)
with st.expander("All flags for this plot and sensor"):
    st.dataframe(pf.sort_values("ts"), use_container_width=True, hide_index=True)
