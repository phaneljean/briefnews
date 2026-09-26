"""
24/7 Railway Worker
Triggers ingest.py every day at 4:30 AM PST (12:30 PM UTC)
"""
import schedule
import time
import os
import sys
import pytz
from datetime import datetime
from ingest import fetch_rss, dedupe, filter_with_gemini, draft_to_beehiiv

def run_ingestion():
    print(f"[{datetime.now(pytz.UTC)}] Starting ingestion run...")
    try:
        items = dedupe(fetch_rss())
        print(f"Fetched {len(items)} items")
        
        if len(items) == 0:
            print("No items fetched. Skipping.")
            return
        
        briefing = filter_with_gemini(items)
        print(f"Generated briefing ({len(briefing)} chars)")
        
        # Save locally for inspection
        os.makedirs("/app/data", exist_ok=True)
        with open("/app/data/latest_briefing.md", "w") as f:
            f.write(briefing)
        print("Saved to /app/data/latest_briefing.md")
        
        # Draft to Beehiiv
        if os.environ.get("PUBLISH_TO_BEEHIIV", "0") == "1":
            result = draft_to_beehiiv(briefing)
            print(f"Draft created on Beehiiv: {result.get('id')}")
        else:
            print("(Beehiiv publish disabled — set PUBLISH_TO_BEEHIIV=1 to enable)")
        
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()

def schedule_job():
    # Schedule at 4:30 AM PST = 12:30 PM UTC
    schedule.every().day.at("12:30").do(run_ingestion)
    print("Scheduled ingestion at 12:30 UTC (4:30 AM PST)")
    
    # Run once at startup if RUN_ON_START=1
    if os.environ.get("RUN_ON_START", "0") == "1":
        print("RUN_ON_START detected — running now...")
        run_ingestion()

if __name__ == "__main__":
    schedule_job()
    print("Worker started. Listening for scheduled jobs...")
    while True:
        schedule.run_pending()
        time.sleep(60)

