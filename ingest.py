"""
Anti-Spectacle Daily Newsletter - Ingestion Engine
FINAL: Chats API + dynamic model discovery - no hardcoded -latest aliases
"""
import feedparser, os, re, requests
from datetime import datetime, timezone

RSS_SOURCES = [
    "https://www.reuters.com/rssFeed/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.apnews.com/apf-topnews",
]

SYSTEM_PROMPT = open(os.path.join(os.path.dirname(__file__), "system_prompt.txt")).read()

GOOGLE_KEY = (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()
if GOOGLE_KEY:
    os.environ["GOOGLE_API_KEY"] = GOOGLE_KEY
    print(f"GOOGLE_API_KEY present: True (length {len(GOOGLE_KEY)})")
else:
    print("WARNING: No GOOGLE_API_KEY set")

_genai_client = None
try:
    from google import genai
    from google.genai import types
    if GOOGLE_KEY:
        _genai_client = genai.Client(
            api_key=GOOGLE_KEY,
            http_options={"api_version": "v1"}
        )
        print(f"Using google-genai v1 API client")
    else:
        print("No key")
except Exception as e:
    print(f"Failed to init client v1: {e}")
    try:
        from google import genai as genai_fallback
        _genai_client = genai_fallback.Client(api_key=GOOGLE_KEY) if GOOGLE_KEY else None
        print(f"Fallback client: {bool(_genai_client)}")
    except Exception as e2:
        print(f"Fallback failed: {e2}")
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
        raise RuntimeError(f"Gemini client not initialized - key present={bool(GOOGLE_KEY)}")
    
    raw = "\n\n".join([f"SOURCE: {i['source']}\nTITLE: {i['title']}\nSUMMARY: {i['summary']}\nLINK: {i['link']}" for i in items[:30]])
    prompt = f"Filter these raw news items into the 4-section briefing format. Return markdown.\n\n{raw}"
    
    # Discover live models - this is source of truth
    live_models = []
    try:
        print("Listing available models for this API key...")
        for m in _genai_client.models.list():
            name = m.name.replace("models/", "")
            if "gemini" in name and ("flash" in name or "pro" in name):
                # skip embedding, vision, etc
                if "embed" in name or "vision" in name or "image" in name:
                    continue
                live_models.append(name)
        print(f"Live models found: {live_models[:20]}")
    except Exception as e:
        print(f"Could not list models: {e}")
    
    # If discovery worked, use those. Otherwise fallback to known-good v1 models
    if live_models:
        candidates = [m for m in live_models if "latest" not in m][:8]
        # Prefer 1.5-flash first
        candidates = sorted(candidates, key=lambda x: (0 if "1.5-flash" in x else 1 if "2.0-flash" in x else 2))
        print(f"Using discovered candidates: {candidates}")
    else:
        candidates = [
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-pro",
        ]
        print(f"Using hardcoded fallback candidates: {candidates}")
    
    last_error = None
    try:
        from google.genai import types
        has_types = True
    except:
        has_types = False
    
    for model_try in candidates:
        try:
            print(f"Trying Chat API with {model_try}")
            if has_types:
                chat = _genai_client.chats.create(
                    model=model_try,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.1
                    )
                )
            else:
                chat = _genai_client.chats.create(model=model_try)
            resp = chat.send_message(prompt)
            print(f"SUCCESS with {model_try} via Chats API - length {len(resp.text)}")
            return resp.text
        except Exception as e:
            print(f"Chat API {model_try} failed: {e}")
            last_error = e
            try:
                print(f"Retrying {model_try} via models.generate_content")
                resp = _genai_client.models.generate_content(
                    model=model_try,
                    contents=f"{SYSTEM_PROMPT}\n\n{prompt}"
                )
                print(f"SUCCESS with {model_try} via generate_content - length {len(resp.text)}")
                return resp.text
            except Exception as e2:
                print(f"Fallback also failed for {model_try}: {e2}")
                last_error = e2
                continue
    
    raise RuntimeError(f"No Gemini model succeeded. Tried {candidates}. Last error: {last_error}")

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
