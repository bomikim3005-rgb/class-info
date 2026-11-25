"""Bay Area Dance Class Map - Streamlit app.

Dated classes come from BigQuery when BQ_PROJECT and GCP_SA_KEY are set (environment
variables or Streamlit secrets), otherwise from classes.csv. Weekly classes always
come from classes.csv. See README.md.

Run:
    pip install -r requirements.txt
    streamlit run app.py
"""
import json
import math
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

import logic as L

st.set_page_config(page_title="Bay Area Dance Map", layout="wide")
st.title("Bay Area Dance Class Map")


def secret(name: str):
    """An environment variable, or else a top-level Streamlit secret (None if neither)."""
    if os.getenv(name):
        return os.getenv(name)
    try:
        return st.secrets.get(name)
    except Exception:  # no secrets.toml
        return None


@st.cache_data
def load_csv(mtimes):
    # mtimes isn't used here: passing the files' modification times makes the
    # cache reload whenever studios.csv or classes.csv is edited. (No leading
    # underscore: st.cache_data ignores parameters named _like_this.)
    return L.load_data()


@st.cache_data(ttl=900, show_spinner="Loading classes from BigQuery...")
def load_bigquery(project: str, key: str):
    info = json.loads(key) if isinstance(key, str) else dict(key)
    return L.load_bigquery_classes(project, info)


studios, classes = load_csv(tuple(p.stat().st_mtime for p in
                                  (Path(L.BASE) / "studios.csv", Path(L.BASE) / "classes.csv")))
source_note = "Classes from classes.csv."
project, key = secret("BQ_PROJECT"), secret("GCP_SA_KEY")
if project and key:
    try:
        # BigQuery holds the dated classes; classes.csv still supplies the weekly ones.
        classes = pd.concat([classes[classes["date"] == ""], load_bigquery(project, key)],
                            ignore_index=True)
        source_note = ("Classes from the studios' booking pages, collected daily into "
                       "BigQuery (past ~60 days and next ~60 days).")
    except Exception as e:  # show the kind of failure, never credentials
        st.warning(f"Couldn't load classes from BigQuery ({type(e).__name__}); "
                   "showing classes.csv instead.")
classes = L.with_time_columns(classes)
st.caption(source_note)

with st.sidebar:
    day = st.date_input("Date", value=date.today(),
                        min_value=date.today() - timedelta(days=60),
                        max_value=date.today() + timedelta(days=60))
    name_query = st.text_input("Class name contains", placeholder="e.g. hip hop, house")
    hours = st.slider("Start time (hour)", 0, 23, (10, 22))
    who = st.text_input("Instructor contains")

todays = L.classes_on(classes, day, name_query, hours, who)
summary = L.studio_summary(studios, todays)

st.subheader(f"{day.strftime('%A, %b %d')}: {len(todays)} classes")

# Studios with classes last, so they draw on top of nearby studios without any.
on_map = summary.sort_values("n_classes")
layers = [
    pdk.Layer("ScatterplotLayer", data=on_map, get_position=["lon", "lat"],
              get_fill_color="color", get_radius=12,
              # Quoted: pydeck reads a bare string as a JavaScript expression.
              radius_units="'pixels'", pickable=True),
    # Where labels would overlap, show only the busier studio's count; the other
    # appears as you zoom in.
    pdk.Layer("TextLayer", data=on_map, get_position=["lon", "lat"], get_text="label",
              get_size=14, get_color=[255, 255, 255, 255],
              extensions=[{"@@type": "CollisionFilterExtension"}],
              get_collision_priority="n_classes", collision_enabled=True),
]
MAP_HEIGHT = 500


def fit_view(lat, lon, height=MAP_HEIGHT, width=800, margin=0.8) -> pdk.ViewState:
    """Center on the middle of the studios' spread and zoom so all of them fit."""
    def merc_y(la):
        return math.log(math.tan(math.pi / 4 + math.radians(la) / 2))
    lon_span = max(lon.max() - lon.min(), 0.01) / 360
    lat_span = max(merc_y(lat.max()) - merc_y(lat.min()), 0.0002) / (2 * math.pi)
    zoom = math.log2(min(width / lon_span, height / lat_span) * margin / 512)
    return pdk.ViewState(latitude=(lat.max() + lat.min()) / 2,
                         longitude=(lon.max() + lon.min()) / 2, zoom=round(zoom, 2))


view = fit_view(studios["lat"], studios["lon"])
st.pydeck_chart(pdk.Deck(
    layers=layers,
    initial_view_state=view,
    tooltip={"html": "<b>{studio}</b><br/>{address}<hr/>{tooltip}",
             # pre-line: show the newlines between classes as line breaks.
             # maxWidth: wrap long class names so the tooltip fits on the map.
             "style": {"backgroundColor": "#222", "color": "white", "whiteSpace": "pre-line",
                       "maxWidth": "260px", "fontSize": "13px"}},
))

for r in summary.sort_values("n_classes", ascending=False).itertuples():
    with st.expander(f"{r.studio} ({r.n_classes} classes)", expanded=r.n_classes > 0):
        st.caption(f"{r.address} | {r.url}")
        if r.notes:
            st.caption(f"Note: {r.notes}")
        rows = todays[todays["studio"] == r.studio]
        if (rows["source"] == "booking page").any():
            st.caption("Live from the studio's booking page.")
        elif not rows.empty:
            st.caption("Regular weekly timetable, which may be out of date: this day is "
                       "beyond what the booking page lists. Check the studio's site.")
        if rows.empty:
            st.write("No classes listed for this day.")
        else:
            cols = ["time_label", "class_name", "instructor", "room", "duration", "price"]
            if rows["notes"].str.strip().any():  # only when there's something to say
                cols.append("notes")
            st.dataframe(
                rows[cols].rename(columns={"time_label": "time", "class_name": "class",
                                           "room": "studio"}),
                hide_index=True, width="stretch",
            )

st.caption("Schedules come from the CSV files and can be out of date. Check the studio site before going.")
