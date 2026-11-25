"""Data loading and filtering for the Bay Area dance class map."""
from datetime import date
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent


CLASS_COLUMNS = ["studio", "weekday", "start", "class_name", "instructor", "room",
                 "duration", "price", "notes", "date"]

# Dated classes from the BigQuery view update_schedules.py --bigquery maintains,
# renamed to match classes.csv.
BIGQUERY_SQL = """
select studio, weekday, start_time as start, class_name,
       coalesce(instructor, '') as instructor, coalesce(room, '') as room,
       coalesce(duration, '') as duration, coalesce(price, '') as price,
       '' as notes, format_date('%Y-%m-%d', class_date) as date
from `{project}.class_info.classes`
"""


def load_data():
    studios = pd.read_csv(BASE / "studios.csv")
    classes = pd.read_csv(BASE / "classes.csv", dtype=str).fillna("")
    for col in CLASS_COLUMNS:  # older files lack some columns
        if col not in classes.columns:
            classes[col] = ""
    return studios, classes[CLASS_COLUMNS]


def load_bigquery_classes(project: str, key_info: dict) -> pd.DataFrame:
    """All dated classes in BigQuery (the last ~60 days and the next ~60)."""
    from google.cloud import bigquery
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(key_info)
    client = bigquery.Client(project=project, credentials=creds)
    rows = [dict(r) for r in client.query(BIGQUERY_SQL.format(project=project)).result()]
    return pd.DataFrame(rows, columns=CLASS_COLUMNS).fillna("")


def with_time_columns(classes: pd.DataFrame) -> pd.DataFrame:
    start = pd.to_datetime(classes["start"], format="%H:%M")
    return classes.assign(hour=start.dt.hour,
                          time_label=start.dt.strftime("%I:%M %p").str.lstrip("0"))


def classes_on(classes: pd.DataFrame, day: date, name_query: str = "",
               hour_range=(0, 23), instructor_query: str = "") -> pd.DataFrame:
    """Classes on `day`, with optional filters.

    Rows with a `date` (YYYY-MM-DD) are live listings from a studio's booking page.
    Rows without one are its regular weekly timetable. On days a studio's live
    listings cover, only those are shown; on other days, its weekly rows are.
    The `source` column says which one each row came from.
    """
    iso = day.isoformat()
    span = classes[classes["date"] != ""].groupby("studio")["date"].agg(["min", "max"])
    live = span.index[(span["min"] <= iso) & (span["max"] >= iso)]
    weekly = ((classes["date"] == "") & (classes["weekday"] == day.strftime("%a"))
              & ~classes["studio"].isin(live))
    df = classes[weekly | (classes["date"] == iso)].copy()
    df["source"] = df["date"].map(lambda d: "booking page" if d else "weekly timetable")
    if name_query.strip():
        df = df[df["class_name"].str.contains(name_query.strip(), case=False, regex=False)]
    df = df[(df["hour"] >= hour_range[0]) & (df["hour"] <= hour_range[1])]
    if instructor_query.strip():
        df = df[df["instructor"].str.contains(instructor_query.strip(), case=False, regex=False)]
    return df.sort_values(["start", "studio"])


def studio_summary(studios: pd.DataFrame, todays: pd.DataFrame) -> pd.DataFrame:
    """One row per studio with class count and a tooltip string for the map.

    The tooltip is plain text with one class per line: the map escapes HTML in
    data values, so a <br/> would show up as text.
    """
    out = studios.copy()

    def tip(name: str) -> str:
        rows = todays[todays["studio"] == name]
        if rows.empty:
            return "No classes listed for this day"
        footer = ("\n(regular weekly timetable, may be out of date)"
                  if (rows["source"] == "weekly timetable").any() else "")
        lines = []
        for r in rows.itertuples():
            # Skip the instructor when the class name already includes it.
            who = (f" - {r.instructor}"
                   if r.instructor and r.instructor.lower() not in r.class_name.lower() else "")
            lines.append(f"{r.time_label}  {r.class_name}{who}")
        return "\n".join(lines) + footer

    out["n_classes"] = out["studio"].map(todays["studio"].value_counts()).fillna(0).astype(int)
    out["tooltip"] = out["studio"].apply(tip)
    # teal when classes exist, gray when none
    out["color"] = out["n_classes"].apply(lambda n: [0, 150, 136, 200] if n else [150, 150, 150, 160])
    # Count only where there are classes: nearby studios' labels would overlap,
    # and the gray dot already says "none today".
    out["label"] = out["n_classes"].apply(lambda n: str(n) if n else "")
    return out
