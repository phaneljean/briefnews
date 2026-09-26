"""
Railway background worker — runs 24/7, triggers ingestion at 4:30 AM PST daily
"""
import os, time, traceback
from datetime import datetime
import pytz

PST = pytz.timezone("America/Los_Angeles")

try:
    from ingest import fetch_rss, dedupe, filter_with_gemini, draft_to_beehiiv, GOOGLE_KEY
    print(f"Imported ingest. GOOGLE_KEY present: {bool(GOOGLE_KEY)}")
except Exception as e:
    print(f"Failed to import ingest: {e}")
    traceback.print_exc()
    raise

def job():
    print(f"[{datetime.now(PST)}] Running daily ingestion...")
    print(f"PUBLISH_TO_BEEHIIV={os.getenv('PUBLISH_TO_BEEHIIV','1')} | GOOGLE_API_KEY set={bool(os.getenv('GOOGLE_API_KEY') or os.getenv('GEMINI_API_KEY'))} | BEEHIIV set={bool(os.getenv('BEEHIIV_API_KEY'))}")
    try:
        items = dedupe(fetch_rss())
        print(f"Fetched {len(items)} items")
        if not items:
            print("No items fetched, skipping Gemini")
            return
        briefing = filter_with_gemini(items)
        print("Briefing generated, length:", len(briefing))
        out_path = "/app/data/latest_briefing.md" if os.path.exists("/app/data") else "latest_briefing.md"
        os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
        with open(out_path,"w") as f:
            f.write(briefing)
        print(f"Saved to {out_path}")
        if os.getenv("PUBLISH_TO_BEEHIIV", "1") == "0":
            print("Draft saved locally (PUBLISH_TO_BEEHIIV=0)")
        elif os.getenv("BEEHIIV_API_KEY"):
            res = draft_to_beehiiv(briefing)
            print("Draft created:", res.get("id"))
        else:
            print("No BEEHIIV_API_KEY, saved locally only")
    except Exception as e:
        print("Error in job:", e)
        traceback.print_exc()

try:
    import schedule
    schedule.every().day.at("04:30").do(job)
    has_schedule = True
except ImportError:
    has_schedule = False
    print("schedule not installed")

print("Worker started — waiting for 4:30 AM PST")

if os.getenv("RUN_ON_START") == "1":
    print("RUN_ON_START=1 -> running now")
    job()
else:
    print("RUN_ON_START not set, will run at 4:30 AM PST")

while True:
    try:
        if has_schedule:
            schedule.run_pending()
        now_pst = datetime.now(PST).strftime("%H:%M")
        if now_pst == "04:30":
            print("Manual 04:30 trigger")
            job()
        time.sleep(60)
    except Exception as e:
        print(f"Loop error: {e}")
        traceback.print_exc()
        time.sleep(30)

