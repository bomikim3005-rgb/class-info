# Bay Area Dance Class Map

A Streamlit map of drop-in dance classes at Bay Area studios. Pick a date to see which studios have classes, then hover over a studio or open its list for times, class names, instructors, rooms, durations and prices.

## Where the classes come from

| Studio | Source |
|---|---|
| City Dance (60 Brady, Annex) | Live: City Dance's Acuity booking page |
| In the Groove Studios | Live: In the Groove's Acuity booking page |
| City Ballet (City Dance) | Weekly timetable in `classes.csv` (not on the booking page) |

`update_schedules.py` opens each booking page in headless Chrome and presses its **More times** button to page through the next 60 days, as a visitor would. Class names are kept as each studio writes them.

## Data

- **`studios.csv`** has one row per studio: name, address, coordinates, website and notes.
- **`classes.csv`** has weekly classes (empty `date`), plus dated classes when you run the scraper locally.
- **BigQuery `class_info.class_listings`** is append-only, with one row per class per daily run. It's partitioned by class date, and each partition expires 60 days after the class (the BigQuery sandbox maximum).
- **BigQuery view `class_info.classes`** holds the latest version of each class. A future class that disappears from its booking page (likely cancelled) is left out. Classes that already happened are kept as they were last seen.

For each studio and date, the app shows dated classes when there are any for that studio's covered range, and the weekly timetable otherwise.

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
