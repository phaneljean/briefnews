"""
24/7 Railway background worker
Triggers ingestion at 4:30 AM PST (12:30 PM UTC) daily
"""
import os
import time
import schedule
from datetime import datetime
import pytz

from ingest import fetch_rss, dedupe, filter_with_gemini, draft_to_beehiiv

PST = pytz.timezone("America/Los_Angeles")

def job():
    now_pst = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{now_pst}] Running daily ingestion...")
    try:
        items = dedupe(fetch_rss())
        print(f"Fetched {len(items)} items")
        
        if len(items) == 0:
            print("No items fetched, skipping.")
            return
        
        briefing = filter_with_gemini(items)
        print(f"Generated briefing ({len(briefing)} chars)")
        
        # Save locally
        os.makedirs("./data", exist_ok=True)
        with open("./data/latest_briefing.md", "w") as f:
            f.write(briefing)
        print("Saved to ./data/latest_briefing.md")
        
        # Push draft to Beehiiv if enabled
        if os.getenv("PUBLISH_TO_BEEHIIV") == "1":
            res = draft_to_beehiiv(briefing)
            if res:
                print(f"Draft created: {res.get('id')}")
        else:
            print("(Beehiiv publish disabled — set PUBLISH_TO_BEEHIIV=1 to enable)")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

def schedule_job():
    # Schedule at 4:30 AM PST = 12:30 PM UTC
    # schedule library uses system time (UTC in Railway)
    schedule.every().day.at("12:30").do(job)
    print("Worker scheduled for 12:30 UTC (4:30 AM PST)")

if __name__ == "__main__":
    schedule_job()
    
    # Run once on deploy for testing if RUN_ON_START=1
    if os.getenv("RUN_ON_START") == "1":
        print("RUN_ON_START=1 -> running now")
        job()
    
    print("Worker started. Listening for scheduled jobs...")
    
    last_run_date = None
    while True:
        schedule.run_pending()
        
        # Log current time every hour
        now = datetime.now(PST)
        if last_run_date != now.date():
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S %Z')}] Waiting for next run...")
            last_run_date = now.date()
        
        time.sleep(60)  # Check every minute

