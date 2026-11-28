# Bay Area Dance Class Map

A Streamlit map of drop-in dance classes at Bay Area studios. Pick a date to see which studios have classes, then hover over a studio or open its list for times, class names, instructors, rooms, durations and prices.

## Where the classes come from

| Studio | Source |
|---|---|
| City Dance (60 Brady, Annex) | Live: City Dance's Acuity booking page |
| In the Groove Studios | Live: In the Groove's Acuity booking page |
| Rae Studios SF (Union Square) and its free UN Plaza classes | Live: Rae's Momence schedule plugin |
| City Ballet (City Dance) | Weekly timetable in `classes.csv` (not on the booking page) |

`update_schedules.py` opens each booking page in headless Chrome and pages through the next 60 days, as a visitor would. On Acuity pages it presses **More times**. Momence's schedule plugin is meant to be embedded on any site, so the script embeds it on a local page and presses **Next Page**. Class names are kept as each studio writes them. Classes marked cancelled on Momence are skipped.

## Data

- **`studios.csv`** has one row per studio: name, address, coordinates, website and notes.
- **`classes.csv`** has weekly classes (empty `date`), plus dated classes when you run the scraper locally.
- **BigQuery `class_info.class_listings`** is append-only, with one row per class per daily run. It's partitioned by class date, and each partition expires 60 days after the class (the BigQuery sandbox maximum).
- **BigQuery view `class_info.classes`** has one row per class. Past classes are frozen: the daily run never loads a class that has already started, and each one keeps the version saved before it began. Future classes come from the latest run. A class that a run listed and a later run (before the class started) didn't is marked `status = 'cancelled'`, and `cancelled_at` is the time of the first run that no longer listed it. Runs happen daily, so that's when the cancellation was noticed, within a day of when it happened. Everything else is `scheduled`. `is_new` marks classes the latest run saw for the first time (ones that only entered the 60-day window don't count).

The app shows past classes in grey italics, crosses out cancelled ones (with the date the cancellation was noticed), and lists newly added classes at the top. For each studio and date, the app shows dated classes when there are any for that studio's covered range, and the weekly timetable otherwise.

## Run locally

Requires Python 3.10+ and Google Chrome.

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -r requirements.txt -r requirements-pipeline.txt
python update_schedules.py          # refresh dated classes in classes.csv (next 60 days)
streamlit run app.py
```

To read from BigQuery locally, set `BQ_PROJECT` and `GCP_SA_KEY` (the key JSON's contents) before `streamlit run`.

## Daily collection (GitHub Actions)

[`.github/workflows/scrape.yml`](.github/workflows/scrape.yml) runs `python update_schedules.py --bigquery` every day at 4:17 am Pacific. Add these repository secrets:

- `BQ_PROJECT`: the Google Cloud project ID.
- `GCP_SA_KEY`: a service account key that can create and write tables (BigQuery Data Editor + BigQuery Job User).

## Deploy the app

On Streamlit Community Cloud, set main file `app.py` and Python 3.12, and add `BQ_PROJECT` and `GCP_SA_KEY` as secrets. Use a **read-only** key here (BigQuery Data Viewer + BigQuery Job User).

## Limitations

- **Live data reaches only what booking pages publish.** Studios using Mindbody or Vagaro block automated browsers, so they can't be added this way.
- **The scraper reads the page as displayed.** If Acuity changes its layout, the scraper finds no classes, logs a warning and saves nothing for that studio, rather than saving bad data.
- **History is limited to 60 days** while the project runs on the BigQuery sandbox.
- **Cancellations are inferred.** Acuity doesn't show cancelled classes; they just disappear. Classes are matched on studio, date, start time and name, so a class that's renamed or moved to another time shows as cancelled, with the new version listed as a new class.
