"""
Anti-Spectacle Daily Newsletter - Ingestion Engine
Runs daily at 4:30 AM PST -> filters -> drafts to Beehiiv
Uses new google-genai SDK with gemini-1.5-flash (stable)
"""
import feedparser, os, re, requests
from datetime import datetime, timezone

RSS_SOURCES = [
    "https://www.reuters.com/rssFeed/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.apnews.com/apf-topnews",
]

SYSTEM_PROMPT = open(os.path.join(os.path.dirname(__file__), "system_prompt.txt")).read()

# Configure with GOOGLE_API_KEY
api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
if not api_key:
    raise ValueError("GOOGLE_API_KEY environment variable is empty or not set")

print(f"GOOGLE_API_KEY present: {bool(api_key)} (length {len(api_key) if api_key else 0})")

# Import new SDK
import google.genai as genai
print("Using google-genai (new SDK) with gemini-1.5-flash")

genai.configure(api_key=api_key)

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
        except Exception as e:
            print(f"Error fetching {url}: {e}")
    return items

def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = re.sub(r'\W+', '', it["title"].lower())[:40]
        if key not in seen:
            seen.add(key); out.append(it)
    return out

def filter_with_gemini(items):
    raw = "\n\n".join([f"SOURCE: {i['source']}\nTITLE: {i['title']}\nSUMMARY: {i['summary']}\nLINK: {i['link']}" for i in items[:30]])
    prompt = f"Filter these raw news items into the 4-section briefing format. Return markdown.\n\n{raw}"
    
    # Try gemini-1.5-flash (stable), fall back to 2.0-flash if needed
    models = ["gemini-1.5-flash", "gemini-2.0-flash"]
    
    for model_name in models:
        try:
            print(f"Trying new client with {model_name}")
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={
                    "system_instruction": SYSTEM_PROMPT,
                    "temperature": 0.1,
                }
            )
            print(f"Success with {model_name}")
            return resp.text
        except Exception as e:
            print(f"Failed with {model_name}: {e}")
            continue
    
    raise Exception("All models failed")

def draft_to_beehiiv(markdown_body):
    # Beehiiv v2 API: create draft post
    pub_id = os.environ.get("BEEHIIV_PUBLICATION_ID")
    api_key_beehiiv = os.environ.get("BEEHIIV_API_KEY")
    
    if not pub_id or not api_key_beehiiv:
        print("Beehiiv credentials missing, skipping draft")
        return None
    
    url = f"https://api.beehiiv.com/v2/publications/{pub_id}/posts"
    headers = {"Authorization": f"Bearer {api_key_beehiiv}", "Content-Type": "application/json"}
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
    print("Imported ingest. GOOGLE_KEY present:", bool(api_key))
    
    items = dedupe(fetch_rss())
    print(f"Fetched {len(items)} items")
    
    briefing = filter_with_gemini(items)
    print(f"Briefing generated, length: {len(briefing)}")
    
    # Save locally
    os.makedirs("./data", exist_ok=True)
    with open("./data/latest_briefing.md", "w") as f:
        f.write(briefing)
    
    publish_enabled = os.environ.get("PUBLISH_TO_BEEHIIV") == "1"
    if publish_enabled:
        result = draft_to_beehiiv(briefing)
        if result:
            print(f"Draft created on Beehiiv: {result.get('id')}")
    else:
        print("Draft saved locally (PUBLISH_TO_BEEHIIV=0)")

