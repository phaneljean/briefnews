"""
The Signal — dashboard + daily scheduler in one process.

Serves the latest and past briefings behind a password, and runs the daily
ingestion at RUN_AT (Pacific) from a background thread. Run it with exactly
one gunicorn worker so there is only ever one scheduler.
"""
import hmac, json, os, threading, time, traceback
from datetime import datetime, timedelta

import markdown as md
import pytz
from flask import Flask, Response, abort, redirect, render_template_string, request, url_for

from ingest import run_once

PACIFIC = pytz.timezone("America/Los_Angeles")
RUN_AT = os.getenv("RUN_AT", "04:30")  # HH:MM Pacific
DATA_DIR = "/app/data" if os.path.isdir("/app/data") else os.path.join(os.path.dirname(__file__), "data")
HISTORY_DIR = os.path.join(DATA_DIR, "briefings")
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

app = Flask(__name__)
_run_lock = threading.Lock()
_state = {"running": False, "started": None, "last_error": None}


# ---------------------------------------------------------------- storage

def save_run(result, trigger):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    now = datetime.now(PACIFIC)
    run_id = now.strftime("%Y-%m-%d-%H%M%S")
    meta = {k: v for k, v in result.items() if k != "briefing"}
    meta.update({"id": run_id, "created": now.isoformat(timespec="seconds"), "trigger": trigger})
    with open(os.path.join(HISTORY_DIR, run_id + ".md"), "w") as f:
        f.write(result["briefing"])
    with open(os.path.join(HISTORY_DIR, run_id + ".json"), "w") as f:
        json.dump(meta, f)
    return run_id


def list_runs(limit=60):
    if not os.path.isdir(HISTORY_DIR):
        return []
    runs = []
    for name in sorted(os.listdir(HISTORY_DIR), reverse=True):
        if name.endswith(".json"):
            with open(os.path.join(HISTORY_DIR, name)) as f:
                runs.append(json.load(f))
            if len(runs) >= limit:
                break
    return runs


def load_run(run_id):
    if not run_id.replace("-", "").isdigit():
        return None
    base = os.path.join(HISTORY_DIR, run_id)
    if not os.path.exists(base + ".md"):
        return None
    with open(base + ".json") as f:
        meta = json.load(f)
    with open(base + ".md") as f:
        meta["briefing"] = f.read()
    return meta


# ---------------------------------------------------------------- running

def job(trigger, push):
    """Run one ingestion; never two at once."""
    if not _run_lock.acquire(blocking=False):
        print(f"Skipped {trigger} run: another run is in progress")
        return
    _state.update(running=True, started=datetime.now(PACIFIC).isoformat(timespec="seconds"), last_error=None)
    print(f"[{datetime.now(PACIFIC)}] Running ingestion ({trigger}, push={push})...")
    try:
        result = run_once(push=push)
        print("Saved run", save_run(result, trigger))
    except Exception as e:
        _state["last_error"] = f"{datetime.now(PACIFIC):%b %d %H:%M} — {e}"
        print("Error in job:", e)
        traceback.print_exc()
    finally:
        _state["running"] = False
        _run_lock.release()


def scheduler():
    push = os.getenv("PUBLISH_TO_BEEHIIV", "1") != "0"
    now = datetime.now(PACIFIC)
    # Started after today's run time: wait for tomorrow rather than re-sending
    # today's briefing on every redeploy (RUN_ON_START=1 forces a run).
    last_run_day = now.date() if now.strftime("%H:%M") > RUN_AT else None
    print(f"Scheduler started — runs daily at {RUN_AT} Pacific (Beehiiv push={push})")
    if os.getenv("RUN_ON_START") == "1":
        job("startup", push)
    while True:
        now = datetime.now(PACIFIC)
        if now.strftime("%H:%M") >= RUN_AT and last_run_day != now.date():
            last_run_day = now.date()
            job("schedule", push)
        time.sleep(30)


def next_run():
    now = datetime.now(PACIFIC)
    hh, mm = map(int, RUN_AT.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = PACIFIC.normalize(target + timedelta(days=1))
    return target


if os.getenv("SCHEDULER", "1") == "1":
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()


# ---------------------------------------------------------------- auth

@app.before_request
def require_password():
    if request.endpoint == "healthz":
        return None
    if not PASSWORD:
        return Response("Set DASHBOARD_PASSWORD to use the dashboard.", 503)
    auth = request.authorization
    if not auth or not hmac.compare_digest(auth.password or "", PASSWORD):
        return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="The Signal"'})
    return None


# ---------------------------------------------------------------- pages

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Signal · {{ title }}</title>
{% if state.running %}<meta http-equiv="refresh" content="10">{% endif %}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap" rel="stylesheet">
<style>
:root{--ink:#111;--muted:#666;--faint:#999;--line:#e7e5e0;--paper:#fbfaf7;--card:#fff;--accent:#111;--warn:#9a3412;--warn-bg:#fff7ed;--ok:#166534;--ok-bg:#f0fdf4}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ink:#eee;--muted:#aaa;--faint:#777;--line:#2c2c2c;--paper:#141414;--card:#1b1b1b;--accent:#eee;--warn:#fdba74;--warn-bg:#2a1a0e;--ok:#86efac;--ok-bg:#0f2417}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 Inter,system-ui,sans-serif}
a{color:inherit}.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
header{border-bottom:1px solid var(--line);background:var(--card)}header .wrap{display:flex;align-items:center;justify-content:space-between;height:60px;gap:12px}
.brand{font:600 20px/1 "Source Serif 4",Georgia,serif;text-decoration:none}.brand span{color:var(--faint);font:500 12px Inter,sans-serif;margin-left:8px}
.grid{display:grid;gap:24px;padding:28px 0 64px}@media(min-width:900px){.grid{grid-template-columns:1fr 300px}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}
.eyebrow{font:500 11px ui-monospace,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
article{font:17px/1.65 "Source Serif 4",Georgia,serif;padding:28px 32px}@media(max-width:600px){article{padding:20px}}
article h1{font-size:30px;line-height:1.15;margin:4px 0 4px}article h2{font:600 13px Inter,sans-serif;letter-spacing:.06em;text-transform:uppercase;border-top:1px solid var(--line);padding-top:18px;margin:28px 0 8px}
article li{margin:8px 0}article a{color:var(--muted);text-decoration:underline;text-underline-offset:2px;font-size:.9em}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.chip{font:11px ui-monospace,Menlo,monospace;border:1px solid var(--line);border-radius:999px;padding:2px 8px;color:var(--muted)}
.chip.ok{background:var(--ok-bg);color:var(--ok);border-color:transparent}.chip.warn{background:var(--warn-bg);color:var(--warn);border-color:transparent}
.side{display:grid;gap:16px;align-content:start}.kv{display:grid;gap:8px;font-size:13px}.kv div{display:flex;justify-content:space-between;gap:10px}.kv span:first-child{color:var(--muted)}
button{font:600 14px Inter,sans-serif;background:var(--accent);color:var(--paper);border:0;border-radius:999px;padding:10px 16px;cursor:pointer;width:100%}button:disabled{opacity:.5;cursor:default}
label.check{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--muted);margin:12px 0}
.list a{display:flex;justify-content:space-between;gap:8px;padding:8px 0;border-top:1px solid var(--line);text-decoration:none;font-size:13px}.list a:first-child{border-top:0}.list a.on{font-weight:600}
.warnbox{background:var(--warn-bg);color:var(--warn);border-radius:10px;padding:12px 14px;font-size:13px;margin-top:14px;overflow-wrap:anywhere}
.empty{color:var(--muted);text-align:center;padding:48px 20px}
</style></head><body>
<header><div class="wrap"><a class="brand" href="/">The Signal<span>briefing desk</span></a>
<span class="chip {{ 'warn' if state.running else 'ok' }}">{{ 'Running now…' if state.running else 'Next run ' + next_run.strftime('%a %b %d, %-I:%M %p %Z') }}</span></div></header>
<main class="wrap grid">
  <div>
  {% if run %}
    <article class="card">
      <div class="eyebrow">{{ run.created[:10] }} · {{ run.trigger }}</div>
      {{ body|safe }}
      <div class="meta" style="font-family:Inter,sans-serif">
        <span class="chip">{{ run.model }}</span>
        <span class="chip">{{ run.items }} items · {{ run.sources|length }} sources</span>
        {% if run.beehiiv %}<span class="chip ok">Beehiiv draft created</span>{% else %}<span class="chip">Not sent to Beehiiv</span>{% endif %}
        {% if run.flagged_links %}<span class="chip warn">{{ run.flagged_links|length }} unverified link{{ 's' if run.flagged_links|length != 1 }}</span>{% endif %}
      </div>
      {% if run.flagged_links %}<div class="warnbox" style="font-family:Inter,sans-serif"><b>Check before sending:</b> these links weren't in the day's feeds.<br>{% for u in run.flagged_links %}{{ u }}<br>{% endfor %}</div>{% endif %}
    </article>
  {% else %}
    <div class="card empty">No briefings yet. Use “Run now”, or wait for the next scheduled run.</div>
  {% endif %}
  </div>
  <aside class="side">
    <form class="card" method="post" action="/run">
      <div class="eyebrow">Run now</div>
      <label class="check"><input type="checkbox" name="push" value="1"> Also create a Beehiiv draft</label>
      <button {{ 'disabled' if state.running }}>{{ 'Running…' if state.running else 'Generate briefing' }}</button>
      {% if state.last_error %}<div class="warnbox">Last run failed: {{ state.last_error }}</div>{% endif %}
    </form>
    <div class="card kv">
      <div class="eyebrow">Schedule</div>
      <div><span>Daily at</span><span>{{ run_at }} Pacific</span></div>
      <div><span>Beehiiv push</span><span>{{ 'On' if push_on else 'Off' }}</span></div>
    </div>
    <div class="card">
      <div class="eyebrow" style="margin-bottom:6px">History</div>
      <div class="list">
      {% for r in runs %}<a href="/b/{{ r.id }}" class="{{ 'on' if run and r.id == run.id }}"><span>{{ r.created[:10] }} {{ r.created[11:16] }}</span><span class="chip">{{ r.trigger }}</span></a>
      {% else %}<div style="color:var(--muted);font-size:13px">Nothing yet.</div>{% endfor %}
      </div>
    </div>
  </aside>
</main></body></html>"""


def render(run):
    body = md.markdown(run["briefing"]) if run else ""
    title = run["created"][:10] if run else "dashboard"
    return render_template_string(PAGE, run=run, body=body, runs=list_runs(), state=_state, title=title,
                                  next_run=next_run(), run_at=RUN_AT,
                                  push_on=os.getenv("PUBLISH_TO_BEEHIIV", "1") != "0")


@app.get("/")
def index():
    runs = list_runs(1)
    return render(load_run(runs[0]["id"]) if runs else None)


@app.get("/b/<run_id>")
def briefing(run_id):
    return render(load_run(run_id) or abort(404))


@app.post("/run")
def run_now():
    if not _state["running"]:
        threading.Thread(target=job, args=("manual", request.form.get("push") == "1"), daemon=True).start()
        time.sleep(0.3)
    return redirect(url_for("index"), 303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "running": _state["running"]}


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 8080)))
