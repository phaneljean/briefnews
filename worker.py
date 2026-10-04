"""
Railway background worker — runs 24/7, triggers ingestion once a day at
4:30 AM Pacific (handles PST/PDT automatically).
"""
import os, time, traceback
from datetime import datetime
import pytz

from ingest import run_once

PACIFIC = pytz.timezone("America/Los_Angeles")
RUN_AT = os.getenv("RUN_AT", "04:30")  # HH:MM Pacific


def job():
    print(f"[{datetime.now(PACIFIC)}] Running daily ingestion...")
    try:
        run_once()
    except Exception as e:
        print("Error in job:", e)
        traceback.print_exc()


now = datetime.now(PACIFIC)
# If we start after today's run time, wait for tomorrow rather than re-sending
# today's briefing on every redeploy (RUN_ON_START=1 forces a run).
last_run_day = now.date() if now.strftime("%H:%M") > RUN_AT else None
print(f"Worker started — runs daily at {RUN_AT} Pacific")
if os.getenv("RUN_ON_START") == "1":
    print("RUN_ON_START=1 -> running now")
    job()

while True:
    now = datetime.now(PACIFIC)
    # One run per Pacific day, at or after RUN_AT, so a missed minute or a
    # restart can't skip or duplicate a day.
    if now.strftime("%H:%M") >= RUN_AT and last_run_day != now.date():
        last_run_day = now.date()
        job()
    time.sleep(30)
