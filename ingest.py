"""
Anti-Spectacle Daily Newsletter - Ingestion Engine
RSS -> dedupe -> Gemini filter -> Beehiiv draft. Uses the google-genai SDK.
"""
import feedparser, os, re, requests, time
from datetime import datetime, timezone

import markdown as md

# (feed URL, label shown to the model, section it mostly feeds, home country)
# Reuters and AP shut down their public RSS feeds, so they're not here.
RSS_SOURCES = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World", "global", "UK"),
    ("https://www.theguardian.com/world/rss", "The Guardian World", "global", "UK"),
    ("https://rss.dw.com/rdf/rss-en-all", "DW", "global", "Germany"),
    ("https://www.france24.com/en/rss", "France 24", "global", "France"),
    ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera", "global", "Qatar"),
    ("https://feeds.npr.org/1001/rss.xml", "NPR News", "policy", "US"),
    ("https://www.pbs.org/newshour/feeds/rss/headlines", "PBS NewsHour", "policy", "US"),
    ("https://www.cnbc.com/id/20910258/device/rss/rss.html", "CNBC Economy", "policy", "US"),
    ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica", "tech", "US"),
    ("https://www.sciencedaily.com/rss/top/science.xml", "ScienceDaily", "tech", "US"),
]
SOURCE_COUNTRY = {label: country for _, label, _, country in RSS_SOURCES}
PER_FEED = 8      # keep the mix balanced across sections
MAX_ITEMS = PER_FEED * len(RSS_SOURCES)  # every feed gets in

SYSTEM_PROMPT = open(os.path.join(os.path.dirname(__file__), "system_prompt.txt")).read()

# Support both env var names - GOOGLE_API_KEY is new, GEMINI_API_KEY is legacy
GOOGLE_KEY = (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()
if GOOGLE_KEY:
    os.environ["GOOGLE_API_KEY"] = GOOGLE_KEY
else:
    print("WARNING: No GOOGLE_API_KEY set")

_genai_client = None
try:
    from google import genai
    from google.genai import types
    if GOOGLE_KEY:
        _genai_client = genai.Client(api_key=GOOGLE_KEY)
except Exception as e:
    print(f"Failed to init google-genai client: {e}")

BEEHIIV_API_KEY = os.environ.get("BEEHIIV_API_KEY", "")
BEEHIIV_PUBLICATION_ID = os.environ.get("BEEHIIV_PUBLICATION_ID", "")


def fetch_rss():
    items = []
    for url, label, section, _country in RSS_SOURCES:
        try:
            feed = feedparser.parse(url, agent="TheSignalBot/1.0 (+https://github.com/phaneljean/briefnews)")
            entries = feed.entries[:PER_FEED]
            if not entries:
                print(f"RSS empty for {label} ({url}) status={feed.get('status')}")
            for e in entries:
                summary = re.sub(r"<[^>]+>", "", e.get("summary", ""))
                items.append({"title": e.get("title", ""), "summary": summary[:800],
                              "link": e.get("link", ""), "source": label, "section": section})
        except Exception as ex:
            print(f"RSS fetch failed for {label}: {ex}")
    return items


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\W+", "", it["title"].lower())[:40]
        if key and key not in seen:
            seen.add(key); out.append(it)
    return out


def _version(name):
    """'models/gemini-2.5-flash' -> (2, 5); unversioned names sort last."""
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def pick_models():
    """Models to try, best first. GEMINI_MODEL wins; otherwise ask the API
    which text models this key can use, so retired models never break a run."""
    override = os.environ.get("GEMINI_MODEL", "").strip()
    usable = []
    try:
        for m in _genai_client.models.list():
            name = m.name.split("/")[-1]
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            if not name.startswith("gemini-") or re.search(r"image|tts|audio|live|embedding|vision", name):
                continue
            usable.append(name)
    except Exception as e:
        print(f"Could not list models: {e}")

    # Prefer stable flash models (fast, cheap), newest first; then flash-lite, then pro.
    def rank(name):
        tier = 0 if re.search(r"flash(?!-lite)", name) else 1 if "flash-lite" in name else 2
        unstable = "preview" in name or "exp" in name
        return (tier, unstable, tuple(-v for v in _version(name)))

    ordered = sorted(set(usable), key=rank)
    return ([override] if override else []) + [m for m in ordered if m != override]


def filter_with_gemini(items):
    if _genai_client is None:
        raise RuntimeError(f"Gemini client not initialized - GOOGLE_API_KEY present={bool(GOOGLE_KEY)}")

    raw = "\n\n".join(f"SOURCE: {i['source']}\nTITLE: {i['title']}\nSUMMARY: {i['summary']}\nLINK: {i['link']}"
                      for i in items[:MAX_ITEMS])
    prompt = ("Filter these raw news items into the 4-section briefing format. Return markdown. "
              "Every bullet must end with the LINK of EVERY item below that reports the same event "
              "(one or more), each in its own parentheses, copied exactly; never invent or alter a link.\n\n" + raw)

    models = pick_models()
    if not models:
        raise RuntimeError("No usable Gemini models for this API key (set GEMINI_MODEL to force one)")
    models = models[:8]
    print(f"Model order: {models}")
    last_error, gone = None, set()
    # Busy models (503/429) are common at peak times: wait and retry the list.
    for attempt in range(3):
        if attempt:
            wait = 30 * attempt
            print(f"All models busy; retrying in {wait}s (round {attempt + 1}/3)")
            time.sleep(wait)
        for model in models:
            if model in gone:
                continue
            try:
                resp = _genai_client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.1))
                if resp.text:
                    print(f"Success with {model}")
                    return resp.text, model
                last_error = f"{model} returned no text"
            except Exception as e:
                msg = str(e)
                print(f"Model {model} failed: {msg[:160]}")
                last_error = e
                if "404" in msg or "NOT_FOUND" in msg:
                    gone.add(model)  # retired or unavailable to this key; don't retry
        if len(gone) == len(models):
            break
    raise RuntimeError(f"No Gemini model succeeded. Tried {models}. Last error: {last_error}")


def unknown_links(briefing, items):
    """Links in the briefing that weren't in today's feeds (possible hallucinations)."""
    known = {i["link"] for i in items}
    found = re.findall(r"https?://[^\s)\]>\"']+", briefing)
    return [u for u in found if u.rstrip(".,;") not in known]


# Domain -> source label, so a link can be attributed even if the model formats it oddly.
SOURCE_DOMAINS = {
    "bbc.co.uk": "BBC World", "bbc.com": "BBC World", "theguardian.com": "The Guardian World",
    "dw.com": "DW", "france24.com": "France 24", "aljazeera.com": "Al Jazeera",
    "npr.org": "NPR News", "pbs.org": "PBS NewsHour", "cnbc.com": "CNBC Economy",
    "arstechnica.com": "Ars Technica", "sciencedaily.com": "ScienceDaily",
}
_URL = re.compile(r"(\]\()?\(?(https?://[^\s)\]>\"']+)\)?")


def source_for(url, by_link=None):
    if by_link and url in by_link:
        return by_link[url]
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return next((name for domain, name in SOURCE_DOMAINS.items() if host == domain or host.endswith("." + domain)), None)


def label_links(briefing, items=()):
    """Turn each source URL, bare or in parentheses, into a short linked name: ([CNBC Economy](url))."""
    by_link = {i["link"]: i["source"] for i in items if i.get("link")}

    def swap(m):
        if m.group(1):  # already a markdown link target "](url)": leave it alone
            return m.group(0)
        url = m.group(2).rstrip(".,;")
        tail = m.group(2)[len(url):]
        name = source_for(url, by_link)
        return f"([{name}]({url})){tail}" if name else m.group(0)

    return _URL.sub(swap, briefing)


def draft_to_beehiiv(markdown_body):
    if os.environ.get("PUBLISH_TO_BEEHIIV", "1") == "0":
        print("PUBLISH_TO_BEEHIIV=0 — skipping Beehiiv push, saving locally only")
        return {"id": "local-only"}
    if not BEEHIIV_API_KEY or not BEEHIIV_PUBLICATION_ID:
        print("Missing BEEHIIV_API_KEY or BEEHIIV_PUBLICATION_ID — saving locally only")
        return {"id": "local-only"}

    # Beehiiv's create-post endpoint takes HTML in body_content (or structured blocks).
    # It's only available on Beehiiv's Max and Enterprise plans.
    url = f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUBLICATION_ID}/posts"
    headers = {"Authorization": f"Bearer {BEEHIIV_API_KEY}", "Content-Type": "application/json"}
    data = {
        "title": f"The Signal — {datetime.now(timezone.utc).strftime('%B %d, %Y')}",
        "subtitle": "All signal, zero noise. Your 3-minute daily briefing.",
        "body_content": md.markdown(markdown_body),
        "status": "draft",
    }
    r = requests.post(url, headers=headers, json=data, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Beehiiv {r.status_code}: {r.text[:300]}")
    body = r.json()
    return body.get("data", body)


def run_once(push=True):
    """Fetch, filter and (optionally) draft to Beehiiv.

    Returns {"briefing", "model", "items", "sources", "flagged_links", "beehiiv"}.
    Saving is the caller's job (see web.py), so history lives in one place.
    """
    items = dedupe(fetch_rss())
    sources = sorted({i["source"] for i in items})
    print(f"Fetched {len(items)} items from {len(sources)} sources")
    if not items:
        raise RuntimeError("No items fetched from any feed")
    briefing, model = filter_with_gemini(items)
    bad = unknown_links(briefing, items)
    if bad:
        print(f"WARNING: briefing has {len(bad)} link(s) not in today's feeds: {bad[:5]}")
    briefing = label_links(briefing, items)
    beehiiv = None
    if push:
        res = draft_to_beehiiv(briefing)
        beehiiv = res.get("id")
        print("Beehiiv:", beehiiv)
    return {"briefing": briefing, "model": model, "items": len(items), "sources": sources,
            "flagged_links": bad, "beehiiv": beehiiv}


if __name__ == "__main__":
    print(run_once(push=os.environ.get("PUBLISH_TO_BEEHIIV", "1") != "0")["briefing"][:1500])
