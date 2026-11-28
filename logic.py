"""Data loading and filtering for the Bay Area dance class map."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import re

import pandas as pd

BASE = Path(__file__).parent


CLASS_COLUMNS = ["studio", "weekday", "start", "class_name", "instructor", "room",
                 "duration", "price", "notes", "date", "is_new", "status", "cancelled_at"]
LOCAL_TZ = "America/Los_Angeles"

# Dated classes from the BigQuery view update_schedules.py --bigquery maintains,
# renamed to match classes.csv.
BIGQUERY_SQL = """
select studio, weekday, start_time as start, class_name,
       coalesce(instructor, '') as instructor, coalesce(room, '') as room,
       coalesce(duration, '') as duration, coalesce(price, '') as price,
       '' as notes, format_date('%Y-%m-%d', class_date) as date,
       if(is_new, '1', '') as is_new,
       if(status = 'cancelled', 'cancelled', '') as status,
       coalesce(format_timestamp('%Y-%m-%d %H:%M', cancelled_at, 'America/Los_Angeles'), '')
           as cancelled_at
from `{project}.class_info.classes`
"""


def load_data():
    studios = pd.read_csv(BASE / "studios.csv")
    classes = pd.read_csv(BASE / "classes.csv", dtype=str).fillna("")
    for col in CLASS_COLUMNS:  # older files lack some columns
        if col not in classes.columns:
            classes[col] = ""
    return studios, classes[CLASS_COLUMNS]


INSTAGRAM_HANDLE = re.compile(r"^[A-Za-z0-9_.]{1,30}$")

# Studio sections are listed in this order (by name prefix), then as in studios.csv.
STUDIO_ORDER = ["In the Groove", "City Dance", "City Ballet", "Rae Studios"]


def load_instructors() -> dict:
    """{instructor name, lowercased: Instagram handle} from instructors.csv.

    The handle may be written as "name", "@name" or an instagram.com link; anything
    that isn't a valid handle is ignored, so it can't become an odd link.
    """
    path = BASE / "instructors.csv"
    if not path.exists():
        return {}
    handles = {}
    for row in pd.read_csv(path, dtype=str).fillna("").itertuples():
        handle = re.sub(r"^(https?://)?(www\.)?instagram\.com/", "", row.instagram.strip())
        handle = handle.split("?")[0].strip("/").lstrip("@")
        if row.instructor.strip() and INSTAGRAM_HANDLE.match(handle):
            handles[row.instructor.strip().lower()] = handle
    return handles


def studio_rank(studio: str) -> int:
    return next((i for i, prefix in enumerate(STUDIO_ORDER) if studio.startswith(prefix)),
                len(STUDIO_ORDER))


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


def classes_between(classes: pd.DataFrame, first: date, last: date, name_query: str = "",
                    hour_range=(0, 23), instructor_query: str = "") -> pd.DataFrame:
    """Classes from `first` through `last`, one row per class occurrence, with filters.

    Rows with a `date` (YYYY-MM-DD) are listings from a studio's booking page. Rows
    without one are its regular weekly timetable. On dates a studio's listings cover,
    only those are used; on other dates, its weekly rows are repeated onto each
    matching weekday. The `source` column says which one each row came from.
    """
    dated = classes[classes["date"] != ""]
    weekly = classes[classes["date"] == ""]
    span = dated.groupby("studio")["date"].agg(["min", "max"])
    now = datetime.now(ZoneInfo(LOCAL_TZ)).strftime("%Y-%m-%d %H:%M")
    frames = [dated[(dated["date"] >= first.isoformat()) & (dated["date"] <= last.isoformat())]
              .assign(source="booking page")]
    if not weekly.empty:
        for i in range((last - first).days + 1):
            day = first + timedelta(days=i)
            iso = day.isoformat()
            live = span.index[(span["min"] <= iso) & (span["max"] >= iso)]
            frames.append(weekly[(weekly["weekday"] == day.strftime("%a"))
                                 & ~weekly["studio"].isin(live)]
                          .assign(date=iso, source="weekly timetable"))
    df = pd.concat(frames, ignore_index=True)
    if name_query.strip():
        df = df[df["class_name"].str.contains(name_query.strip(), case=False, regex=False)]
    df = df[(df["hour"] >= hour_range[0]) & (df["hour"] <= hour_range[1])]
    if instructor_query.strip():
        df = df[df["instructor"].str.contains(instructor_query.strip(), case=False, regex=False)]
    df = df.assign(date_label=pd.to_datetime(df["date"]).dt.strftime("%a, %b %-d"),
                   # Started already (local time); "YYYY-MM-DD HH:MM" strings sort as times.
                   is_past=(df["date"] + " " + df["start"]) < now)
    return df.sort_values(["date", "start", "studio"]).reset_index(drop=True)


def new_classes(classes: pd.DataFrame) -> pd.DataFrame:
    """Upcoming classes the latest daily run saw for the first time."""
    now = datetime.now(ZoneInfo(LOCAL_TZ)).strftime("%Y-%m-%d %H:%M")
    df = classes[(classes["is_new"] == "1") & ((classes["date"] + " " + classes["start"]) >= now)]
    df = df.assign(date_label=pd.to_datetime(df["date"]).dt.strftime("%a, %b %-d"))
    return df.sort_values(["date", "start", "studio"]).reset_index(drop=True)


def date_bounds(classes: pd.DataFrame, today: date) -> tuple:
    """First and last dates with dated classes (today if there are none)."""
    dated = classes.loc[classes["date"] != "", "date"]
    if dated.empty:
        return today, today
    return date.fromisoformat(dated.min()), date.fromisoformat(dated.max())


TOOLTIP_CLASSES = 6  # classes listed in a map tooltip before "...and N more"


def studio_summary(studios: pd.DataFrame, picked: pd.DataFrame) -> pd.DataFrame:
    """One row per studio with class count and a tooltip string for the map.

    The tooltip is plain text with one class per line: the map escapes HTML in
    data values, so a <br/> would show up as text.
    """
    out = studios.copy()

    def tip(name: str) -> str:
        rows = picked[picked["studio"] == name]
        if rows.empty:
            return "No classes listed for these dates"
        lines = []
        for r in rows.head(TOOLTIP_CLASSES).itertuples():
            # Skip the instructor when the class name already includes it.
            who = (f" - {r.instructor}"
                   if r.instructor and r.instructor.lower() not in r.class_name.lower() else "")
            lines.append(f"{r.date_label}, {r.time_label}  {r.class_name}{who}")
        if len(rows) > TOOLTIP_CLASSES:
            lines.append(f"...and {len(rows) - TOOLTIP_CLASSES} more (see the list below the map)")
        if (rows["source"] == "weekly timetable").any():
            lines.append("(includes the regular weekly timetable, which may be out of date)")
        return "\n".join(lines)

    picked = picked[picked["status"] != "cancelled"]  # the map counts classes still on
    out["n_classes"] = out["studio"].map(picked["studio"].value_counts()).fillna(0).astype(int)
    out["tooltip"] = out["studio"].apply(tip)
    # teal when classes exist, gray when none
    out["color"] = out["n_classes"].apply(lambda n: [0, 150, 136, 200] if n else [150, 150, 150, 160])
    # Count only where there are classes: nearby studios' labels would overlap,
    # and the gray dot already says "none".
    out["label"] = out["n_classes"].apply(lambda n: str(n) if n else "")
    return out
