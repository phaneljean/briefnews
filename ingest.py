"""
Anti-Spectacle Daily Newsletter - Ingestion Engine
Runs daily at 4:30 AM PST -> filters -> drafts to Beehiiv
Supports both google-generativeai (legacy) and google-genai (new SDK)
"""
import feedparser, os, re, requests
from datetime import datetime, timezone

# Auto-detect SDK
try:
    import google.generativeai as genai
    genai.configure(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    model = genai.GenerativeModel("gemini-2.5-flash")
    SDK_TYPE = "google-generativeai (legacy)"
except (ImportError, AttributeError):
    import google.genai as genai
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    model = client.models
    SDK_TYPE = "google-genai (new)"

RSS_SOURCES = [
    "https://www.reuters.com/rssFeed/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.apnews.com/apf-topnews",
]

SYSTEM_PROMPT = open(os.path.join(os.path.dirname(__file__), "system_prompt.txt")).read()

def fetch_rss():
    items = []
    for url in RSS_SOURCES:
        feed = feedparser.parse(url)
        for e in feed.entries[:20]:
            title = e.get("title","")
            summary = re.sub(r'<[^>]+>', '', e.get("summary",""))
            link = e.get("link","")
            items.append({"title": title, "summary": summary[:800], "link": link, "source": url})
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
    
    if SDK_TYPE == "google-generativeai (legacy)":
        # Legacy SDK
        generative_model = genai.GenerativeModel("gemini-2.5-flash", system_instruction=SYSTEM_PROMPT,
            generation_config={"temperature": 0.1})
        resp = generative_model.generate_content(prompt)
        return resp.text
    else:
        # New SDK
        resp = model.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "system_instruction": SYSTEM_PROMPT,
                "temperature": 0.1,
            }
        )
        return resp.text

def draft_to_beehiiv(markdown_body):
    # Beehiiv v2 API: create draft post
    url = f"https://api.beehiiv.com/v2/publications/{os.environ.get('BEEHIIV_PUBLICATION_ID')}/posts"
    headers = {"Authorization": f"Bearer {os.environ.get('BEEHIIV_API_KEY')}", "Content-Type": "application/json"}
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
    print(f"Using {SDK_TYPE}")
    items = dedupe(fetch_rss())
    print(f"Fetched {len(items)} items")
    briefing = filter_with_gemini(items)
    print(briefing[:500])
    # Uncomment to publish draft:
    # result = draft_to_beehiiv(briefing)
    # print("Draft created:", result.get("id"))
    os.makedirs("./data", exist_ok=True)
    with open("./data/latest_briefing.md","w") as f:
        f.write(briefing)
    print("Draft saved locally to ./data/latest_briefing.md")

