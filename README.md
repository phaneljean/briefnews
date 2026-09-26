# The Signal — Anti-Spectacle Daily Newsletter Ingestion Engine

A Python-based ingestion pipeline that fetches news from Reuters, BBC, and AP News, filters for signal over noise using Google Gemini, and drafts briefings to Beehiiv.

## How It Works

1. **Fetch**: Pulls latest items from RSS feeds (Reuters, BBC World, AP News)
2. **Dedupe**: Removes duplicate headlines
3. **Filter**: Sends through Gemini with a system prompt to strip clickbait, speculation, and political theater
4. **Organize**: Structures into 4 sections:
   - Core Policy & Economy (3 facts)
   - Global Affairs (2-3 events)
   - Tech & Science (2 breakthroughs)
   - The Noise Filter (1 sentence on what was excluded)
5. **Draft**: Posts to Beehiiv as a draft for your 5-minute review

## Local Setup

```bash
pip install -r requirements.txt

# Set env vars
export GEMINI_API_KEY="your-key"
export BEEHIIV_API_KEY="your-key"
export BEEHIIV_PUBLICATION_ID="your-id"

# Test run
python ingest.py

# Or run the 24/7 worker
python worker.py
```

## Railway Deployment

1. Push this repo to GitHub
2. Create a new Railway service from this repo
3. Set environment variables:
   - `GEMINI_API_KEY`
   - `BEEHIIV_API_KEY`
   - `BEEHIIV_PUBLICATION_ID`
   - `RUN_ON_START=1` (for first test)
4. Deploy — worker runs 24/7 and triggers at 4:30 AM PST (12:30 PM UTC)

## Configuration

- **Schedule**: 4:30 AM PST = 12:30 PM UTC (in `worker.py`)
- **RSS Sources**: Configurable in `ingest.py` (Reuters, BBC, AP by default)
- **Beehiiv Drafting**: Set `PUBLISH_TO_BEEHIIV=1` to auto-publish (defaults to drafts only)
- **System Prompt**: Edit `system_prompt.txt` to tune the filter logic

