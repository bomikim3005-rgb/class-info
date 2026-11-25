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
from_bigquery = False
project, key = secret("BQ_PROJECT"), secret("GCP_SA_KEY")
if project and key:
    try:
        # BigQuery holds the dated classes; classes.csv still supplies the weekly ones.
        classes = pd.concat([classes[classes["date"] == ""], load_bigquery(project, key)],
                            ignore_index=True)
        source_note = ("Classes from the studios' booking pages, collected daily into "
                       "BigQuery (past ~60 days and next ~60 days).")
        from_bigquery = True
    except Exception as e:  # show the kind of failure, never credentials
        st.warning(f"Couldn't load classes from BigQuery ({type(e).__name__}); "
                   "showing classes.csv instead.")
classes = L.with_time_columns(classes)
st.caption(source_note)

today = date.today()
first_date, last_date = L.date_bounds(classes, today)
with st.sidebar:
    picked_range = st.date_input(
        "Dates", value=(today, today + timedelta(days=6)),  # the next 7 days
        min_value=min(first_date, today - timedelta(days=60)),
        max_value=max(last_date, today + timedelta(days=60)),
        help="Defaults to the next 7 days, starting today. Pick a start and an end date.")
    name_query = st.text_input("Class name contains", placeholder="e.g. hip hop, house")
    hours = st.slider("Start time (hour)", 0, 23, (0, 23))
    who = st.text_input("Instructor contains")

# While picking, the widget briefly returns only the start date.
start_day, end_day = (picked_range if len(picked_range) == 2
                      else (picked_range[0], picked_range[0]))
picked = L.classes_between(classes, start_day, end_day, name_query, hours, who)
summary = L.studio_summary(studios, picked)

# Rows for classes that have already happened: greyed out, in italics.
PAST_STYLE = "background-color: rgba(128, 128, 128, 0.22); color: #9a9a9a; font-style: italic"


def styled(table: pd.DataFrame, past: pd.Series):
    return table.style.apply(lambda row: [PAST_STYLE if past.loc[row.name] else ""] * len(row),
                             axis=1)


st.markdown("#### Newly added classes")
if not from_bigquery:
    st.caption("Lists upcoming classes that weren't in yesterday's daily run. "
               "Needs the BigQuery history, so it shows once the app reads from BigQuery.")
else:
    fresh = L.new_classes(classes)
    if fresh.empty:
        st.caption("No new classes since yesterday's run.")
    else:
        one = len(fresh) == 1
        st.caption(f"{len(fresh)} upcoming class{'' if one else 'es'} that "
                   f"{'was' if one else 'were'}n't in yesterday's run.")
        st.dataframe(
            fresh[["date_label", "time_label", "studio", "class_name", "instructor", "price"]]
            .rename(columns={"date_label": "date", "time_label": "time", "class_name": "class"}),
            hide_index=True, width="stretch", height=min(250, (len(fresh) + 1) * 35 + 3),
        )

st.subheader(f"{len(picked):,} class{'' if len(picked) == 1 else 'es'}")

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

STUDIO_BOX_HEIGHT = 680  # px; a studio's section scrolls inside this when it's taller
ROW_PX = 35               # st.dataframe row height


def section_height(rows) -> int:
    """Rough pixel height of a studio's section: captions, then per date a heading
    and a table showing all its rows."""
    per_day = rows.groupby("date").size()
    return 120 + int(sum(45 + (n + 1) * ROW_PX + 20 for n in per_day))


for r in summary.sort_values("n_classes", ascending=False).itertuples():
    rows = picked[picked["studio"] == r.studio]
    with st.expander(f"{r.studio} ({r.n_classes} class{'' if r.n_classes == 1 else 'es'})",
                     expanded=r.n_classes > 0):
        # Fixed height only when the section would be taller; short ones keep their size.
        box = (st.container(height=STUDIO_BOX_HEIGHT, border=False)
               if section_height(rows) > STUDIO_BOX_HEIGHT else st.container())
        with box:
            st.caption(f"{r.address} | {r.url}")
            if r.notes:
                st.caption(f"Note: {r.notes}")
            sources = set(rows["source"])
            if sources == {"booking page"}:
                st.caption("From the studio's booking page.")
            elif sources == {"weekly timetable"}:
                st.caption("Regular weekly timetable, which may be out of date. Check the "
                           "studio's site.")
            elif sources:
                st.caption("From the studio's booking page. Dates beyond what it lists use "
                           "the regular weekly timetable, which may be out of date.")
            if rows.empty:
                st.write("No classes listed for these dates.")
                continue
            cols = ["time_label", "class_name", "instructor", "room", "duration", "price"]
            if rows["notes"].str.strip().any():  # only when there's something to say
                cols.append("notes")
            # One table per date, under that date's heading. st.table (a plain HTML
            # table) shows every row, so only the section scrolls, and unlike
            # st.dataframe it applies the italic style for past classes.
            for _, day_rows in rows.groupby("date", sort=True):
                st.markdown(f"**{day_rows['date_label'].iloc[0]}** · {len(day_rows)} "
                            f"class{'' if len(day_rows) == 1 else 'es'}")
                st.table(
                    styled(day_rows[cols].rename(columns={"time_label": "time",
                                                          "class_name": "class",
                                                          "room": "studio"}),
                           day_rows["is_past"]),
                    hide_index=True,
                )

st.caption("Schedules come from the CSV files and can be out of date. Check the studio site before going.")
