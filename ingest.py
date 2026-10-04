"""
Anti-Spectacle Daily Newsletter - Ingestion Engine
RSS -> dedupe -> Gemini filter -> Beehiiv draft. Uses the google-genai SDK.
"""
import feedparser, os, re, requests
from datetime import datetime, timezone

import markdown as md

# (feed URL, label shown to the model, section it mostly feeds)
# Reuters and AP shut down their public RSS feeds, so they're not here.
RSS_SOURCES = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World", "global"),
    ("https://www.theguardian.com/world/rss", "The Guardian World", "global"),
    ("https://rss.dw.com/rdf/rss-en-all", "DW", "global"),
    ("https://feeds.npr.org/1001/rss.xml", "NPR News", "policy"),
    ("https://www.pbs.org/newshour/feeds/rss/headlines", "PBS NewsHour", "policy"),
    ("https://www.cnbc.com/id/20910258/device/rss/rss.html", "CNBC Economy", "policy"),
    ("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica", "tech"),
    ("https://www.sciencedaily.com/rss/top/science.xml", "ScienceDaily", "tech"),
]
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
    for url, label, section in RSS_SOURCES:
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
              "Every bullet must end with the LINK of the item it comes from, copied exactly; "
              "never invent or alter a link.\n\n" + raw)

    models = pick_models()
    if not models:
        raise RuntimeError("No usable Gemini models for this API key (set GEMINI_MODEL to force one)")
    print(f"Model order: {models[:5]}")
    last_error = None
    for model in models[:5]:
        try:
            resp = _genai_client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.1))
            if resp.text:
                print(f"Success with {model}")
                return resp.text, model
            last_error = f"{model} returned no text"
        except Exception as e:
            print(f"Model {model} failed: {e}")
            last_error = e
    raise RuntimeError(f"No Gemini model succeeded. Tried {models[:5]}. Last error: {last_error}")


def unknown_links(briefing, items):
    """Links in the briefing that weren't in today's feeds (possible hallucinations)."""
    known = {i["link"] for i in items}
    found = re.findall(r"https?://[^\s)\]>\"']+", briefing)
    return [u for u in found if u.rstrip(".,;") not in known]


def label_links(briefing, items):
    """Turn each bare source URL into a short linked source name, e.g. (CNBC Economy)."""
    by_link = {i["link"]: i["source"] for i in items if i["link"]}

    def swap(m):
        url = m.group(0).rstrip(".,;")
        tail = m.group(0)[len(url):]
        name = by_link.get(url)
        return f"([{name}]({url})){tail}" if name else m.group(0)

    # Bare URLs only, not ones already inside markdown link syntax "(url)".
    return re.sub(r"(?<!\()https?://[^\s)\]>\"']+", swap, briefing)


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
