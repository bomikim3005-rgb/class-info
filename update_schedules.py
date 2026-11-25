"""Refresh classes.csv with each studio's current dated listings from its booking page.

City Dance and In the Groove both publish classes through Acuity Scheduling pages
that are built with JavaScript, so this renders each page in headless Google Chrome
and reads the class list from the result.

Acuity shows 15 classes at a time, so this presses the page's "More times" button
to page forward until it has DAYS_AHEAD days of classes, as a visitor would.

For each studio, this replaces its dated rows (date column filled) and leaves its
weekly rows (date empty) alone; the app uses the weekly rows for days the listings
don't reach.

With --bigquery, the classes are appended to BigQuery instead (see load_to_bigquery),
so classes stay available after they've happened. GitHub Actions runs that daily.

Run:
    python update_schedules.py              # update classes.csv
    python update_schedules.py --dry-run    # print what would be written
    python update_schedules.py --days 14    # look 14 days ahead instead of 60
    python update_schedules.py --bigquery   # append to BigQuery; needs BQ_PROJECT and
                                            # GOOGLE_APPLICATION_CREDENTIALS (key file)
"""
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import websocket  # websocket-client: talks to Chrome's DevTools protocol

CHROME = (os.getenv("CHROME_PATH") or shutil.which("google-chrome")  # Linux (GitHub Actions)
          or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CSV_PATH = Path(__file__).parent / "classes.csv"
FIELDS = ["studio", "weekday", "start", "class_name", "instructor", "room", "duration",
          "price", "notes", "date"]
DAYS_AHEAD = 60

BQ_DATASET = "class_info"
BQ_TABLE = "class_listings"   # append-only: every class seen by every run
BQ_VIEW = "classes"           # latest version of each class (see VIEW_SQL)
BQ_LOCATION = "US"
RETENTION_DAYS = 60           # BigQuery sandbox maximum
LOCAL_TZ = "America/Los_Angeles"
MORE_BUTTON = 'button[aria-label="More times"]'

DATE_RE = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
                     r"(\w+) (\d+)(?:st|nd|rd|th), (\d{4})$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2} [AP]M$")
LENGTH_RE = re.compile(r"^(.+?) @ \$(\d+(?:\.\d\d)?)$")


def parse_groove(title: str):
    """'Int/Adv Hip Hop w/ Leslie P; Studio 2 with Studios 1 & 2'
    -> (studio, class name as written, instructor or "", room or "")."""
    name = re.sub(r"\s+with Studios? [\d &]+$", "", title).strip()  # booking-calendar label
    m = re.search(r"\b(?:w/|with)\s*([^;]+)|^master class:\s*(.+)$", name, re.I)
    instructor = (m.group(1) or m.group(2)).strip() if m else ""
    room = re.search(r";\s*(Studio \d+)", name)
    return "In the Groove Studios", name, instructor, room.group(1) if room else ""


CITY_DANCE_LOCATIONS = [  # text in the booking page's location -> studios.csv name
    ("ANNEX", "City Dance - Annex"),
    ("1420 HARRISON", "City Dance - Annex"),
    ("60 BRADY", "City Dance - 60 Brady"),
    ("OTIS", "City Ballet (City Dance)"),
    ("BALLET", "City Ballet (City Dance)"),
]


def parse_city_dance(title: str):
    """'Beginner Choreo | Sila Poe with class at 60 BRADY | STUDIO B'
    -> (studio, class name, instructor, room)."""
    left, _, where = title.rpartition(" with class at ")
    if not left:
        return None
    name, _, instructor = left.rpartition(" | ")
    if not name:
        name, instructor = instructor, ""
    studio = next((s for key, s in CITY_DANCE_LOCATIONS if key in where.upper()), None)
    if studio is None:
        print(f"  skipped (unknown City Dance location {where!r}): {title}", file=sys.stderr)
        return None
    room = where.rpartition(" | ")[2].strip().title() if " | " in where else ""
    return studio, name.strip(), instructor.strip(), room


SOURCES = [
    {"name": "City Dance", "parse": parse_city_dance,
     "studios": {s for _, s in CITY_DANCE_LOCATIONS},
     "url": "https://app.acuityscheduling.com/schedule.php?owner=22968233&template=class"},
    {"name": "In the Groove", "parse": parse_groove,
     "studios": {"In the Groove Studios"},
     "url": "https://app.acuityscheduling.com/schedule.php?owner=30172961&template=class"},
]


class Browser:
    """A headless Chrome tab driven over the DevTools protocol."""

    def __init__(self):
        self.profile = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--remote-debugging-port=0",
             f"--user-data-dir={self.profile.name}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(self.profile.name) / "DevToolsActivePort"
        for _ in range(100):  # Chrome writes its port here once it's listening
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.2)
        port = port_file.read_text().split()[0]
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
        page = next(t for t in tabs if t["type"] == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
        self.msg_id = 0

    def call(self, method, **params):
        self.msg_id += 1
        self.ws.send(json.dumps({"id": self.msg_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.msg_id:
                return msg.get("result", {})

    def js(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        return result.get("result", {}).get("value")

    def text(self) -> str:
        return self.js("document.body ? document.body.innerText : ''") or ""

    def wait_for(self, condition, timeout=30) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if condition():
                return True
            time.sleep(0.5)
        return False

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.profile.cleanup()


def booking_pages(url: str, until: date) -> list:
    """Visible text of each page of listings, pressing "More times" until `until`."""
    browser = Browser()
    try:
        browser.call("Page.navigate", url=url)
        if not browser.wait_for(lambda: any(TIME_RE.match(l) for l in lines(browser.text()))):
            return []
        pages = []
        while True:
            text = browser.text()
            pages.append(lines(text))
            days = [listing_date(l) for l in pages[-1] if DATE_RE.match(l)]
            if not days or max(days) > until:
                break
            clicked = browser.js(f"""(() => {{ const b = [...document.querySelectorAll('{MORE_BUTTON}')].pop();
                                     if (!b) return false; b.click(); return true; }})()""")
            # Stop at the end of the schedule, or if the next page never loads.
            if not clicked or not browser.wait_for(lambda: browser.text() != text, timeout=20):
                break
            browser.wait_for(lambda: any(TIME_RE.match(l) for l in lines(browser.text())), 10)
        return pages
    finally:
        browser.close()


def lines(text: str) -> list:
    return [line.strip() for line in text.splitlines() if line.strip()]


def listing_date(line: str) -> date:
    m = DATE_RE.match(line)
    return datetime.strptime(f"{m[2]} {m[3]} {m[4]}", "%B %d %Y").date()


def parse(lines: list, parse_title) -> tuple:
    """Return (rows, number of listings seen on the page)."""
    rows, seen, day, i = [], 0, None, 0
    while i < len(lines):
        if DATE_RE.match(lines[i]):
            day = listing_date(lines[i])
        elif day and TIME_RE.match(lines[i]) and i + 2 < len(lines):
            seen += 1
            parsed = parse_title(lines[i + 1])
            if parsed:
                studio, name, instructor, room = parsed
                length = LENGTH_RE.match(lines[i + 2])
                rows.append({
                    "studio": studio,
                    "weekday": day.strftime("%a"),
                    "start": datetime.strptime(lines[i], "%I:%M %p").strftime("%H:%M"),
                    "class_name": name,
                    "instructor": instructor,
                    "room": room,
                    "duration": length[1].replace(" minutes", " min") if length else "",
                    "price": f"${float(length[2]):.0f}" if length else "",
                    "notes": "",
                    "date": day.isoformat(),
                })
            i += 3
            continue
        i += 1
    return rows, seen


def scrape(source: dict, until: date) -> list:
    """All of a source's classes from today through `until`, each listed once."""
    rows = {}
    for page in booking_pages(source["url"], until):
        for r in parse(page, source["parse"])[0]:
            if r["date"] <= until.isoformat():
                rows[(r["studio"], r["date"], r["start"], r["class_name"])] = r
    return sorted(rows.values(), key=lambda r: (r["date"], r["start"], r["studio"]))


# The latest version of each class. A future class that's missing from its studio's
# latest run was taken off the booking page (likely cancelled), so it's left out.
# Classes stop being listed once they start, so a class from before the latest run's
# date is kept as it was last seen.
VIEW_SQL = """
create or replace view `{dataset}.{view}` as
with latest_run as (
    select source, max(scraped_at) as run_at
    from `{dataset}.{table}`
    group by source
),
ranked as (
    select
        *,
        min(scraped_at) over (partition by studio, class_date, start_time, class_name)
            as first_seen_at,
        row_number() over (partition by studio, class_date, start_time, class_name
                           order by scraped_at desc) as rn
    from `{dataset}.{table}`
)
select
    r.source, r.studio, r.class_date, r.weekday, r.start_time, r.class_name,
    r.instructor, r.room, r.duration, r.price,
    r.first_seen_at, r.scraped_at as last_seen_at
from ranked as r
inner join latest_run as l on r.source = l.source
where r.rn = 1
  and (r.scraped_at = l.run_at
       or r.class_date < date(l.run_at, '{tz}'))
"""


def load_to_bigquery(rows: list, project: str) -> None:
    """Append this run's classes to BigQuery and refresh the `classes` view.

    Load jobs are free and work in the BigQuery sandbox (streaming inserts don't).
    Sandbox tables are deleted 60 days after creation, so, as in job-posting-monitor,
    the table is partitioned (here by class date: each class is kept until 60 days
    after it happens) and every run pushes the table's own expiry forward.
    """
    from google.cloud import bigquery  # only needed for --bigquery runs

    client = bigquery.Client(project=project)
    dataset = bigquery.Dataset(f"{project}.{BQ_DATASET}")
    dataset.location = BQ_LOCATION
    client.create_dataset(dataset, exists_ok=True)

    dest = f"{project}.{BQ_DATASET}.{BQ_TABLE}"
    schema = [bigquery.SchemaField(name, kind, mode="REQUIRED" if name in
                                   ("source", "studio", "class_date", "start_time",
                                    "class_name", "scraped_at") else "NULLABLE")
              for name, kind in [
                  ("source", "STRING"), ("studio", "STRING"), ("class_date", "DATE"),
                  ("weekday", "STRING"), ("start_time", "STRING"), ("class_name", "STRING"),
                  ("instructor", "STRING"), ("room", "STRING"), ("duration", "STRING"),
                  ("price", "STRING"), ("scraped_at", "TIMESTAMP")]]
    job = client.load_table_from_json(
        rows, dest,
        job_config=bigquery.LoadJobConfig(
            schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            # Used when the load creates the table; must match an existing table.
            time_partitioning=bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY, field="class_date",
                expiration_ms=RETENTION_DAYS * 24 * 3600 * 1000),
        ))
    job.result()  # wait; raises if the load failed
    client.query(VIEW_SQL.format(dataset=f"{project}.{BQ_DATASET}", table=BQ_TABLE,
                                 view=BQ_VIEW, tz=LOCAL_TZ)).result()
    try:
        table = client.get_table(dest)
        table.expires = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS - 1)
        client.update_table(table, ["expires"])
    except Exception as e:  # the data is loaded; don't fail the run, but make it visible
        print(f"::warning::Could not extend table expiry ({type(e).__name__}: {e}). "
              "The table may be deleted when it reaches its expiration date.")
    print(f"Loaded {len(rows)} classes into {dest}.")


def bigquery_rows(source: dict, rows: list, scraped_at: datetime) -> list:
    return [{"source": source["name"], "studio": r["studio"], "class_date": r["date"],
             "weekday": r["weekday"], "start_time": r["start"], "class_name": r["class_name"],
             "instructor": r["instructor"] or None, "room": r["room"] or None,
             "duration": r["duration"] or None, "price": r["price"] or None,
             "scraped_at": scraped_at.isoformat()}
            for r in rows]


def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else DAYS_AHEAD
    until = date.today() + timedelta(days=days)
    to_bigquery = "--bigquery" in sys.argv
    project = os.getenv("BQ_PROJECT")
    if to_bigquery and not project:
        sys.exit("--bigquery needs BQ_PROJECT set, e.g. export BQ_PROJECT=my-project-id")
    new_rows, bq_rows, refreshed = [], [], set()
    for source in SOURCES:
        scraped_at = datetime.now(timezone.utc)
        rows = scrape(source, until)
        if not rows:
            # "::warning::" shows up as an annotation on the GitHub Actions run page.
            print(f"::warning::{source['name']}: no classes found on the booking page; "
                  "nothing saved for it this run.")
            continue
        new_rows += rows
        bq_rows += bigquery_rows(source, rows, scraped_at)
        refreshed |= source["studios"]
        print(f"{source['name']}: {len(rows)} classes, {rows[0]['date']} to {rows[-1]['date']}.")
    if not new_rows:
        sys.exit("No classes found on any booking page.")
    if to_bigquery:
        load_to_bigquery(bq_rows, project)
        return
    if "--dry-run" in sys.argv:
        csv.DictWriter(sys.stdout, FIELDS, lineterminator="\n").writerows(new_rows)
        return
    with open(CSV_PATH, newline="") as f:
        # Keep weekly rows, and dated rows for studios whose page couldn't be read.
        kept = [r for r in csv.DictReader(f) if not (r["date"] and r["studio"] in refreshed)]
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, lineterminator="\n", restval="")
        w.writeheader()
        w.writerows(kept + new_rows)
    print(f"Updated {CSV_PATH.name}.")


if __name__ == "__main__":
    main()
