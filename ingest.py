"""
Anti-Spectacle Daily Newsletter - Ingestion Engine
NEW SDK ONLY: google-genai (Client API)
No genai.configure() - that method does not exist in new SDK
"""
import feedparser, os, re, requests
from datetime import datetime, timezone

RSS_SOURCES = [
    "https://www.reuters.com/rssFeed/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.apnews.com/apf-topnews",
]

SYSTEM_PROMPT = open(os.path.join(os.path.dirname(__file__), "system_prompt.txt")).read()

# Support both env var names - GOOGLE_API_KEY is new, GEMINI_API_KEY is legacy
GOOGLE_KEY = (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()
if GOOGLE_KEY:
    os.environ["GOOGLE_API_KEY"] = GOOGLE_KEY
    print(f"GOOGLE_API_KEY present: True (length {len(GOOGLE_KEY)})")
else:
    print("WARNING: No GOOGLE_API_KEY set")

# --- NEW SDK ONLY ---
_genai_client = None
_MODEL_CANDIDATES = ["gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-2.0-flash", "gemini-pro"]

try:
    from google import genai
    if GOOGLE_KEY:
        _genai_client = genai.Client(api_key=GOOGLE_KEY)
        print(f"Using google-genai new SDK with {_MODEL_CANDIDATES[0]}")
    else:
        print("No key for new SDK")
except Exception as e:
    print(f"Failed to init google-genai client: {e}")
    _genai_client = None

BEEHIIV_API_KEY = os.environ.get("BEEHIIV_API_KEY", "")
BEEHIIV_PUBLICATION_ID = os.environ.get("BEEHIIV_PUBLICATION_ID", "")

def fetch_rss():
    items = []
    for url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:20]:
                title = e.get("title","")
                summary = re.sub(r'<[^>]+>', '', e.get("summary",""))
                link = e.get("link","")
                items.append({"title": title, "summary": summary[:800], "link": link, "source": url})
        except Exception as ex:
            print(f"RSS fetch failed for {url}: {ex}")
    return items

def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = re.sub(r'\W+', '', it["title"].lower())[:40]
        if key not in seen:
            seen.add(key); out.append(it)
    return out

def filter_with_gemini(items):
    if not items:
        return "No items fetched today."
    
    if _genai_client is None:
        raise RuntimeError(f"Gemini client not initialized - GOOGLE_API_KEY present={bool(GOOGLE_KEY)}")
    
    raw = "\n\n".join([f"SOURCE: {i['source']}\nTITLE: {i['title']}\nSUMMARY: {i['summary']}\nLINK: {i['link']}" for i in items[:30]])
    prompt = f"Filter these raw news items into the 4-section briefing format. Return markdown.\n\n{raw}"
    
    last_error = None
    for model_try in _MODEL_CANDIDATES:
        try:
            print(f"Trying new client with {model_try}")
            resp = _genai_client.models.generate_content(
                model=model_try,
                contents=prompt,
                config={"system_instruction": SYSTEM_PROMPT, "temperature": 0.1}
            )
            print(f"Success with {model_try}")
            return resp.text
        except Exception as e:
            print(f"Model {model_try} failed: {e}")
            last_error = e
            try:
                resp = _genai_client.models.generate_content(
                    model=model_try,
                    contents=f"{SYSTEM_PROMPT}\n\n{prompt}"
                )
                print(f"Success with {model_try} (fallback prompt)")
                return resp.text
            except Exception as e2:
                print(f"Fallback also failed for {model_try}: {e2}")
                last_error = e2
                continue
    
    raise RuntimeError(f"No Gemini model succeeded. Tried {_MODEL_CANDIDATES}. Last error: {last_error}")

def draft_to_beehiiv(markdown_body):
    if os.environ.get("PUBLISH_TO_BEEHIIV", "1") == "0":
        print("PUBLISH_TO_BEEHIIV=0 — skipping Beehiiv push, saving locally only")
        return {"id": "local-only"}
    
    if not BEEHIIV_API_KEY or not BEEHIIV_PUBLICATION_ID:
        print("Missing BEEHIIV_API_KEY or BEEHIIV_PUBLICATION_ID — saving locally only")
        return {"id": "local-only"}
    
    url = f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUBLICATION_ID}/posts"
    headers = {"Authorization": f"Bearer {BEEHIIV_API_KEY}", "Content-Type": "application/json"}
    data = {
        "title": f"The Signal — {datetime.now(timezone.utc).strftime('%B %d, %Y')}",
        "subtitle": "All signal, zero noise. Your 3-minute daily briefing.",
        "content": markdown_body,
        "status": "draft",
    }
    r = requests.post(url, headers=headers, json=data, timeout=30)
    r.raise_for_status()
    return r.json()

if __name__ == "__main__":
    items = dedupe(fetch_rss())
    print(f"Fetched {len(items)} items")
    briefing = filter_with_gemini(items)
    print(briefing[:500])
    out_path = "/app/data/latest_briefing.md" if os.path.exists("/app/data") else "latest_briefing.md"
    with open(out_path,"w") as f:
        f.write(briefing)
    print(f"Saved to {out_path}")

