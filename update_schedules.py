"""Refresh classes.csv with each studio's current dated listings from its booking page.

Studios publish classes through booking systems whose pages are built with
JavaScript, so this renders each page in headless Google Chrome and reads the class
list from the result:

- Acuity Scheduling (City Dance, In the Groove): shows 15 classes at a time; this
  presses the page's "More times" button to page forward, as a visitor would.
- Momence (Rae Studios): its schedule plugin is meant to be embedded on any web
  page, so this embeds it on a local page and presses its "Next Page" button.

Either way, paging continues until it has DAYS_AHEAD days of classes.

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
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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

# Acuity
DATE_RE = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
                     r"(\w+) (\d+)(?:st|nd|rd|th), (\d{4})$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2} [AP]M$")
LENGTH_RE = re.compile(r"^(.+?) @ \$(\d+(?:\.\d\d)?)$")

# Momence
MOMENCE_DATE_RE = re.compile(r"^(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, \w+ \d{1,2}, \d{4}$")
MOMENCE_NEXT = 'button[aria-label^="Next Page"]'
MOMENCE_TYPES = {"CLASS", "WORKSHOP", "EVENT", "COURSE"}
MOMENCE_EMBED = """<!doctype html><html><body>
<div id="momence-plugin-host-schedule"></div>
<script async type="module" host_id="{host_id}" teacher_ids="[]" location_ids="[]"
        tag_ids="[]" src="https://momence.com/plugin/host-schedule/host-schedule.js"></script>
</body></html>"""


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
    # Show tickets put the venue where the instructor goes ("... | 60 BRADY").
    if any(key in instructor.upper() for key, _ in CITY_DANCE_LOCATIONS):
        instructor = ""
    return studio, name.strip(), instructor.strip(), room


RAE_LOCATIONS = {  # Momence location -> studios.csv name; anything else is the studio
    "UN Plaza Fitness Court": "Rae Studios - UN Plaza (free)",
}

SOURCES = [
    {"name": "City Dance", "kind": "acuity", "parse": parse_city_dance,
     "studios": {s for _, s in CITY_DANCE_LOCATIONS},
     "url": "https://app.acuityscheduling.com/schedule.php?owner=22968233&template=class"},
    {"name": "In the Groove", "kind": "acuity", "parse": parse_groove,
     "studios": {"In the Groove Studios"},
     "url": "https://app.acuityscheduling.com/schedule.php?owner=30172961&template=class"},
    {"name": "Rae Studios", "kind": "momence", "host_id": 51741,
     "default_studio": "Rae Studios SF", "locations": RAE_LOCATIONS,
     "studios": {"Rae Studios SF", *RAE_LOCATIONS.values()}},
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


def collect_pages(url: str, until: date, date_re, to_date, next_button: str) -> list:
    """Visible text of each page of listings, pressing `next_button` until `until`."""
    browser = Browser()
    has_dates = lambda: any(date_re.match(l) for l in lines(browser.text()))
    try:
        browser.call("Page.navigate", url=url)
        if not browser.wait_for(has_dates):
            return []
        pages = []
        while True:
            text = browser.text()
            pages.append(lines(text))
            days = [to_date(l) for l in pages[-1] if date_re.match(l)]
            if not days or max(days) > until:
                break
            clicked = browser.js(f"""(() => {{ const b = [...document.querySelectorAll('{next_button}')].pop();
                                     if (!b || b.disabled) return false; b.click(); return true; }})()""")
            # Stop at the end of the schedule, or if the next page never loads.
            if not clicked or not browser.wait_for(lambda: browser.text() != text, timeout=20):
                break
            browser.wait_for(has_dates, 10)
        return pages
    finally:
        browser.close()


def momence_date(line: str) -> date:
    return datetime.strptime(line, "%A, %B %d, %Y").date()


class EmbedServer:
    """Serves a local page embedding a Momence schedule plugin, on a free local port."""

    def __init__(self, host_id: int):
        page = MOMENCE_EMBED.format(host_id=int(host_id)).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *args):  # keep the run's output quiet
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def close(self):
        self.server.shutdown()


def minutes_text(minutes: int) -> str:
    """90 -> '1 hour 30 min', matching how Acuity writes durations."""
    hours, mins = divmod(minutes, 60)
    parts = [f"{hours} hour{'s' if hours > 1 else ''}" if hours else "", f"{mins} min" if mins else ""]
    return " ".join(p for p in parts if p)


def parse_momence(lines: list, source: dict) -> list:
    """Classes from a Momence plugin page. Each one reads:
    CLASS, name, instructor, (bio and description), date, start, -, end, 'N min',
    location, price, status ('Book now', 'Finished', 'Cancelled', ...)."""
    rows = []
    for k, line in enumerate(lines):
        if not MOMENCE_DATE_RE.match(line) or k + 7 >= len(lines):
            continue
        start, dash, _end, length, location, price, status = lines[k + 1:k + 8]
        if not TIME_RE.match(start) or dash != "-" or status.lower() == "cancelled":
            continue
        j = k - 1
        while j >= 0 and lines[j].upper() not in MOMENCE_TYPES:
            j -= 1
        if j < 0 or j + 1 >= k:
            continue
        name = lines[j + 1]
        # The instructor follows the name, unless the class has none listed.
        maybe = lines[j + 2] if j + 2 < k else ""
        instructor = (maybe if maybe and len(maybe) < 60 and ":" not in maybe
                      and maybe not in ("Show bio", "Show more") else "")
        day = momence_date(line)
        minutes = re.match(r"^(\d+) min$", length)
        studio = source["locations"].get(location, source["default_studio"])
        rows.append({
            "studio": studio,
            "weekday": day.strftime("%a"),
            "start": datetime.strptime(start, "%I:%M %p").strftime("%H:%M"),
            "class_name": name,
            "instructor": instructor,
            "room": "" if location in source["locations"] else location,
            "duration": minutes_text(int(minutes[1])) if minutes else "",
            "price": price if price.startswith("$") or price.lower() == "free" else "",
            "notes": "",
            "date": day.isoformat(),
        })
    return rows


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
    if source["kind"] == "momence":
        server = EmbedServer(source["host_id"])
        try:
            pages = collect_pages(server.url, until, MOMENCE_DATE_RE, momence_date, MOMENCE_NEXT)
        finally:
            server.close()
        found = [r for page in pages for r in parse_momence(page, source)]
    else:
        pages = collect_pages(source["url"], until, DATE_RE, listing_date, MORE_BUTTON)
        found = [r for page in pages for r in parse(page, source["parse"])[0]]
    for r in found:
        if r["date"] <= until.isoformat():
            rows[(r["studio"], r["date"], r["start"], r["class_name"])] = r
    return sorted(rows.values(), key=lambda r: (r["date"], r["start"], r["studio"]))


# One row per class: the version to show.
#
# - Past classes are frozen: only rows scraped before a class started count (and
#   main() never loads a class that has already started), so later runs can't
#   change them.
# - Cancelled classes: each run saves a studio's whole schedule, so a class that a
#   run listed and a later run (before the class started) didn't is cancelled.
#   Momence's own "Cancelled" classes are skipped by the scraper, so they drop out
#   the same way. status is 'cancelled' and cancelled_at is the first run that no
#   longer listed it; that run must reach the class's date, so a run whose paging
#   stopped early can't cancel classes past where it stopped. A class that comes
#   back in a later run is scheduled again. Classes are matched on studio, date,
#   start time and name, so a renamed or moved class shows as cancelled plus new.
# - Everything else is 'scheduled', in the version from the last run that listed it.
# - is_new: first seen within the last two days of runs (today's or yesterday's), and
#   missing from the run before that. Classes that only entered the DAYS_AHEAD window
#   don't count, and neither does anything in a studio's very first run or anything
#   cancelled.
VIEW_SQL = """
create or replace view `{dataset}.{view}` as
with listings as (
    select
        *,
        timestamp(datetime(class_date, parse_time('%H:%M', start_time)), '{tz}')
            as class_start
    from `{dataset}.{table}`
),
runs as (  -- one row per studio source per daily run
    select
        source,
        scraped_at as run_at,
        max(class_date) as last_date,  -- how far that run's listings reached
        row_number() over (partition by source order by scraped_at desc) as recency
    from listings
    group by source, scraped_at
),
before_start as (
    select
        *,
        min(scraped_at) over (partition by studio, class_date, start_time, class_name)
            as first_seen_at,
        row_number() over (partition by studio, class_date, start_time, class_name
                           order by scraped_at desc) as rn
    from listings
    where scraped_at <= class_start
),
latest as (
    select * from before_start where rn = 1
),
missed_run as (  -- the first run after the class was last listed that left it out
    select l.studio, l.class_date, l.start_time, l.class_name, min(r.run_at) as run_at
    from latest as l
    inner join runs as r
        on r.source = l.source and r.run_at > l.scraped_at
       and r.run_at <= l.class_start and r.last_date >= l.class_date
    group by l.studio, l.class_date, l.start_time, l.class_name
),
newest_run as (
    select source, run_at from runs where recency = 1
),
prior_run as (  -- the last run before the one that first saw the class
    select l.studio, l.class_date, l.start_time, l.class_name, max(r.run_at) as run_at
    from latest as l
    inner join runs as r on r.source = l.source and r.run_at < l.first_seen_at
    group by l.studio, l.class_date, l.start_time, l.class_name
)
select
    l.source, l.studio, l.class_date, l.weekday, l.start_time, l.class_name,
    l.instructor, l.room, l.duration, l.price, l.class_start,
    l.first_seen_at, l.scraped_at as last_seen_at,
    l.class_start < current_timestamp() as is_past,
    if(m.run_at is null, 'scheduled', 'cancelled') as status,
    m.run_at as cancelled_at,
    coalesce(m.run_at is null and p.run_at is not null
             -- 36 hours: today's and yesterday's daily runs, with room for GitHub delays
             and l.first_seen_at > timestamp_sub(n.run_at, interval 36 hour)
             and l.class_date <= date_add(date(p.run_at, '{tz}'),
                                          interval {days} day), false) as is_new
from latest as l
left join missed_run as m
    on l.studio = m.studio and l.class_date = m.class_date
   and l.start_time = m.start_time and l.class_name = m.class_name
left join newest_run as n on l.source = n.source
left join prior_run as p
    on l.studio = p.studio and l.class_date = p.class_date
   and l.start_time = p.start_time and l.class_name = p.class_name
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
                                 view=BQ_VIEW, tz=LOCAL_TZ, days=DAYS_AHEAD)).result()
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
        # Classes that have already started (e.g. "Finished" ones on Momence) are
        # past: never load them, so a past class keeps the version saved before it.
        now_local = scraped_at.astimezone(ZoneInfo(LOCAL_TZ)).strftime("%Y-%m-%d %H:%M")
        bq_rows += bigquery_rows(source, [r for r in rows if f"{r['date']} {r['start']}" > now_local],
                                 scraped_at)
        refreshed |= source["studios"]
        print(f"{source['name']}: {len(rows)} classes, {rows[0]['date']} to {rows[-1]['date']}.")
    if not new_rows:
        sys.exit("No classes found on any booking page.")
    if to_bigquery:
        if not bq_rows:
            sys.exit("No upcoming classes to load.")
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
