"""
The Signal — dashboard + daily scheduler in one process.

Serves the latest and past briefings behind a password, and runs the daily
ingestion at RUN_AT (Pacific) from a background thread. Run it with exactly
one gunicorn worker so there is only ever one scheduler.
"""
import hmac, json, math, os, re, threading, time, traceback
from datetime import datetime, timedelta

import markdown as md
import pytz
import requests
from flask import Flask, Response, abort, redirect, render_template, request, url_for

from ingest import SOURCE_COUNTRY, label_links, run_once

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

PUBLIC_ENDPOINTS = {"home", "issue", "archive", "subscribe", "healthz", "static"}


@app.before_request
def require_password():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not PASSWORD:
        return Response("Set DASHBOARD_PASSWORD to use the dashboard.", 503)
    auth = request.authorization
    if not auth or not hmac.compare_digest(auth.password or "", PASSWORD):
        return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="The Signal"'})
    return None


# ---------------------------------------------------------------- pages




def render(run):
    body = md.markdown(run["briefing"]) if run else ""
    title = run["created"][:10] if run else "dashboard"
    return render_template("admin.html", run=run, body=body, runs=list_runs(), state=_state, title=title,
                                  next_run=next_run(), run_at=RUN_AT,
                                  push_on=os.getenv("PUBLISH_TO_BEEHIIV", "1") != "0")


@app.get("/admin")
def admin():
    runs = list_runs(1)
    return render(load_run(runs[0]["id"]) if runs else None)


@app.get("/admin/b/<run_id>")
def briefing(run_id):
    return render(load_run(run_id) or abort(404))


@app.post("/run")
def run_now():
    if not _state["running"]:
        threading.Thread(target=job, args=("manual", request.form.get("push") == "1"), daemon=True).start()
        time.sleep(0.3)
    return redirect(url_for("admin"), 303)


# ---------------------------------------------------------------- public site

def public_runs(limit=60):
    """Issues safe to show publicly: no links that weren't in the day's feeds."""
    return [r for r in list_runs(limit * 2) if not r.get("flagged_links")][:limit]


_SENTENCE_END = re.compile(r"(?<!\b[A-Z])\.(?=\s+[A-Z(\"“])")


def _bold_lead(bullet):
    """Bold the first sentence of a story, newspaper style."""
    m = _SENTENCE_END.search(bullet)
    if not m or m.end() > 220:
        return bullet
    rest = bullet[m.end():].strip()
    if not re.sub(r"\(?\[[^\]]*\]\([^)]*\)\)?|https?://\S+", "", rest).strip():
        return bullet  # one-sentence story: nothing to set apart
    return f"**{bullet[:m.end()].strip()}** {rest}"


_LINKED_SOURCE = re.compile(r"\(\[([^\]]+)\]\((https?://[^)\s]+)\)\)")
COUNTRY_ORDER = ["US", "UK", "Germany", "France", "Qatar"]


def _story(line, noise):
    """One bullet -> {html, sources, countries, ...} for a story card."""
    sources, seen = [], set()
    for label, url in _LINKED_SOURCE.findall(line):
        if label not in seen:
            seen.add(label)
            sources.append({"name": label, "url": url, "country": SOURCE_COUNTRY.get(label, "Other")})
    text = _LINKED_SOURCE.sub("", line).strip().rstrip(".") + "."
    text = re.sub(r"\s+([.,;])", r"\1", text)
    counts = {}
    for src in sources:
        counts[src["country"]] = counts.get(src["country"], 0) + 1
    countries = [{"name": c, "n": counts[c], "pct": round(100 * counts[c] / len(sources))}
                 for c in COUNTRY_ORDER + sorted(set(counts) - set(COUNTRY_ORDER)) if c in counts]
    return {"html": md.markdown(text if noise else _bold_lead(text))[3:-4], "sources": sources,
            "countries": countries,
            # Blindspot: several outlets covered it, but all from one country.
            "blindspot": countries[0]["name"] if len(sources) >= 2 and len(countries) == 1 else None}


def issue_view(run):
    """Split a briefing into sections of story cards with coverage data."""
    text = label_links(run["briefing"])  # also attributes older issues saved with bare links
    sections = []
    for chunk in re.split(r"^##\s+", text, flags=re.M)[1:]:
        title, _, body = chunk.partition("\n")
        m = re.match(r"(\d+)[.)]?\s*(.*)", title.strip())
        num, name = (m.group(1), m.group(2)) if m else (str(len(sections) + 1), title.strip())
        lines = [re.sub(r"^\s*[-*]\s+", "", ln) for ln in body.splitlines() if re.match(r"^\s*[-*]\s+", ln)]
        noise = "noise" in name.lower()
        sections.append({"n": num.zfill(2), "title": name, "noise": noise,
                         "slug": re.sub(r"[^a-z]+", "-", name.lower()).strip("-"),
                         "stories": [_story(ln, noise) for ln in lines]})
    stories = [st for sec in sections if not sec["noise"] for st in sec["stories"]]
    words = len(re.sub(r"https?://\S+|\[|\]|\(|\)", " ", text).split())
    all_runs = sorted(r["id"] for r in list_runs(10000))
    created = datetime.fromisoformat(run["created"])
    return {"id": run["id"], "sections": sections, "stories": len(stories),
            "articles": sum(len(st["sources"]) for st in stories),
            "multi": sum(1 for st in stories if len(st["sources"]) >= 2),
            "blindspots": [st for st in stories if st["blindspot"]],
            "minutes": max(1, math.ceil(words / 230)), "sources": run.get("sources", []),
            "number": all_runs.index(run["id"]) + 1 if run["id"] in all_runs else None,
            "date": created.strftime("%A, %B %-d, %Y"), "short_date": created.strftime("%b %-d")}


def subscribe_to_beehiiv(email):
    pub, key = os.getenv("BEEHIIV_PUBLICATION_ID", ""), os.getenv("BEEHIIV_API_KEY", "")
    if not pub or not key:
        raise RuntimeError("Subscriptions aren't set up yet.")
    r = requests.post(f"https://api.beehiiv.com/v2/publications/{pub}/subscriptions",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"email": email, "reactivate_existing": False, "send_welcome_email": True,
                            "utm_source": "website", "referring_site": "the-signal-home"}, timeout=15)
    if not r.ok:
        print(f"Beehiiv subscribe failed {r.status_code}: {r.text[:200]}")
        raise RuntimeError("We couldn't add you right now. Please try again in a minute.")


_recent_signups = {}  # ip -> [timestamps], a light guard against form spam


@app.get("/")
def home():
    runs = public_runs(1)
    view = issue_view(load_run(runs[0]["id"])) if runs else None
    return render_template("home.html", issue=view, status=request.args.get("s"),
                           message=request.args.get("m"), **_coverage_legend())


def _coverage_legend():
    by_country = {}
    for label, country in SOURCE_COUNTRY.items():
        by_country.setdefault(country, []).append(label)
    order = [c for c in COUNTRY_ORDER if c in by_country] + sorted(set(by_country) - set(COUNTRY_ORDER))
    return {"countries": [{"name": c, "outlets": by_country[c]} for c in order],
            "source_total": len(SOURCE_COUNTRY), "country_total": len(by_country)}


@app.get("/issue/<run_id>")
def issue(run_id):
    run = load_run(run_id)
    if not run or run.get("flagged_links"):
        abort(404)
    return render_template("issue.html", issue=issue_view(run))


@app.get("/archive")
def archive():
    items = []
    for r in public_runs(90):
        created = datetime.fromisoformat(r["created"])
        items.append({"id": r["id"], "date": created.strftime("%a, %b %-d, %Y"), "items": r.get("items")})
    return render_template("archive.html", runs=items)


@app.post("/subscribe")
def subscribe():
    email = (request.form.get("email") or "").strip().lower()
    if request.form.get("website"):  # honeypot field real people never fill in
        return redirect(url_for("home", s="ok") + "#subscribe", 303)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", email) or len(email) > 254:
        return redirect(url_for("home", s="err", m="Enter a valid email address.") + "#subscribe", 303)
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0]).strip()
    now = time.time()
    hits = [t for t in _recent_signups.get(ip, []) if now - t < 3600]
    if len(hits) >= 5:
        return redirect(url_for("home", s="err", m="Too many sign-ups from this network. Try again later.") + "#subscribe", 303)
    _recent_signups[ip] = hits + [now]
    try:
        subscribe_to_beehiiv(email)
    except RuntimeError as e:
        return redirect(url_for("home", s="err", m=str(e)) + "#subscribe", 303)
    return redirect(url_for("home", s="ok") + "#subscribe", 303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "running": _state["running"]}


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 8080)))
