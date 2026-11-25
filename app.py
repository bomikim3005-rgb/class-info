"""Bay Area Dance Class Map - Streamlit app.

Dated classes come from BigQuery when BQ_PROJECT and GCP_SA_KEY are set (environment
variables or Streamlit secrets), otherwise from classes.csv. Weekly classes always
come from classes.csv. See README.md.

Run:
    pip install -r requirements.txt
    streamlit run app.py
"""
import html
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


@st.cache_data
def load_instructors(mtime):
    # mtime only makes the cache reload when instructors.csv is edited.
    return L.load_instructors()


studios, classes = load_csv(tuple(p.stat().st_mtime for p in
                                  (Path(L.BASE) / "studios.csv", Path(L.BASE) / "classes.csv")))
instructors_csv = Path(L.BASE) / "instructors.csv"
instagram = load_instructors(instructors_csv.stat().st_mtime if instructors_csv.exists() else 0)
from_bigquery = False
project, key = secret("BQ_PROJECT"), secret("GCP_SA_KEY")
if project and key:
    try:
        # BigQuery holds the dated classes; classes.csv still supplies the weekly ones.
        classes = pd.concat([classes[classes["date"] == ""], load_bigquery(project, key)],
                            ignore_index=True)
        from_bigquery = True
    except Exception as e:  # show the kind of failure, never credentials
        st.warning(f"Couldn't load classes from BigQuery ({type(e).__name__}); "
                   "showing classes.csv instead.")
classes = L.with_time_columns(classes)

today = date.today()
first_date, last_date = L.date_bounds(classes, today)
with st.sidebar:
    anytime = st.toggle("Anytime", help="Every date with classes, past and upcoming.")
    picked_range = st.date_input(
        "Dates", value=(today, today + timedelta(days=6)),  # the next 7 days
        min_value=min(first_date, today - timedelta(days=60)),
        max_value=max(last_date, today + timedelta(days=60)),
        disabled=anytime,
        help="Defaults to the next 7 days, starting today. Pick a start and an end date.")
    name_query = st.text_input("Class name contains", placeholder="e.g. hip hop, house")
    hours = st.slider("Start time (hour)", 0, 23, (0, 23))
    who = st.text_input("Instructor contains")

if anytime:
    start_day, end_day = first_date, last_date
else:
    # While picking, the widget briefly returns only the start date.
    start_day, end_day = (picked_range if len(picked_range) == 2
                          else (picked_range[0], picked_range[0]))
picked = L.classes_between(classes, start_day, end_day, name_query, hours, who)
summary = L.studio_summary(studios, picked)

# Class tables are plain HTML, so instructor names can link to Instagram and past
# classes can be greyed out in italics. Every value is escaped.
st.markdown("""<style>
table.classes {width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-bottom: 0.5rem}
table.classes th, table.classes td {text-align: left; padding: 0.4rem 0.6rem;
    border-bottom: 1px solid rgba(128, 128, 128, 0.25)}
table.classes th {font-weight: 500; opacity: 0.7}
table.classes tr.past td {background: rgba(128, 128, 128, 0.22); color: #9a9a9a; font-style: italic}
</style>""", unsafe_allow_html=True)


def instructor_html(name: str) -> str:
    handle = instagram.get(name.strip().lower())
    if not handle:
        return html.escape(name)
    return (f'<a href="https://www.instagram.com/{handle}/" target="_blank" rel="noopener noreferrer" '
            f'title="@{handle} on Instagram">{html.escape(name)}</a>')


def class_table(rows: pd.DataFrame, columns: dict) -> None:
    """columns: {column in rows: header}. Rows with is_past set are greyed out."""
    head = "".join(f"<th>{html.escape(h)}</th>" for h in columns.values())
    body = []
    for _, r in rows.iterrows():
        cells = "".join(f"<td>{instructor_html(r[c]) if c == 'instructor' else html.escape(str(r[c]))}</td>"
                        for c in columns)
        body.append(f'<tr class="{"past" if r.get("is_past", False) else ""}">{cells}</tr>')
    st.markdown(f'<table class="classes"><thead><tr>{head}</tr></thead>'
                f'<tbody>{"".join(body)}</tbody></table>', unsafe_allow_html=True)


st.markdown("#### Newly added classes")
if not from_bigquery:
    st.caption("Upcoming classes added to the studios' schedules in the last two days "
               "show here once the daily collection has a few days of history.")
else:
    fresh = L.new_classes(classes)
    if fresh.empty:
        st.caption("No classes added in the last two days.")
    else:
        one = len(fresh) == 1
        st.caption(f"{len(fresh)} upcoming class{'' if one else 'es'} added in the last two days.")
        with st.container(height=min(260, 60 + 37 * len(fresh)), border=False):
            class_table(fresh, {"date_label": "date", "time_label": "time", "studio": "studio",
                                "class_name": "class", "instructor": "instructor", "price": "price"})

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


# In the Groove, then City Dance, then Rae Studios (see L.STUDIO_ORDER).
ordered = summary.assign(rank=summary["studio"].map(L.studio_rank)).sort_values("rank", kind="stable")
for r in ordered.itertuples():
    rows = picked[picked["studio"] == r.studio]
    with st.expander(f"{r.studio} ({r.n_classes} class{'' if r.n_classes == 1 else 'es'})",
                     expanded=r.n_classes > 0):
        # Fixed height only when the section would be taller; short ones keep their size.
        box = (st.container(height=STUDIO_BOX_HEIGHT, border=False)
               if section_height(rows) > STUDIO_BOX_HEIGHT else st.container())
        with box:
            st.caption(f"{r.address} | {r.url}")
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
            cols = {"time_label": "time", "class_name": "class", "instructor": "instructor",
                    "room": "studio", "duration": "duration", "price": "price"}
            if rows["notes"].str.strip().any():  # only when there's something to say
                cols["notes"] = "notes"
            # One table per date, under that date's heading, showing every row, so
            # only the studio's section scrolls.
            for _, day_rows in rows.groupby("date", sort=True):
                st.markdown(f"**{day_rows['date_label'].iloc[0]}** · {len(day_rows)} "
                            f"class{'' if len(day_rows) == 1 else 'es'}")
                class_table(day_rows, cols)

st.caption("Schedules come from the studios' booking pages and can change. Check the "
           "studio's site before going. Instructor names link to Instagram where known "
           "(see instructors.csv).")
