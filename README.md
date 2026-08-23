# Webpage Chat Assistant

A Chrome extension that lets you chat with any webpage in real time — ask questions about a product listing, article, or documentation page and get answers grounded in what's actually on the page, powered by an LLM backend.

**Example use case:** you're browsing a product on Amazon or Flipkart and don't want to read the entire listing. Open the side panel and ask "what's the price after offers?" or "what are the cons?" — get a direct answer instead of scanning the page yourself.


## How it works

1. A content script extracts clean text from whatever page you're viewing
2. That content is sent to a FastAPI backend, which holds it as context for the conversation
3. Questions you ask in the side panel are answered by an LLM (via Groq) using that page as grounding
4. Each browser tab keeps its own independent chat session — switching tabs shows that tab's conversation, not a mix of pages

## Tech stack

- **Extension:** Chrome Manifest V3, Side Panel API, vanilla JS
- **Backend:** FastAPI (Python)
- **LLM orchestration:** LangChain (`ChatGroq`, `RunnableWithMessageHistory`)
- **Inference:** Groq API (free tier)

## Project structure

```
webpage-chat-assistant/
├── backend/
│   ├── main.py            # FastAPI app: /ingest and /chat endpoints
│   ├── requirements.txt
│   └── .env.example
└── extension/
    ├── manifest.json       # MV3 config, side panel + permissions
    ├── background.js       # Service worker, relays page-load events per tab
    ├── content.js           # Extracts page text, responds to on-demand requests
    ├── sidepanel.html/.css/.js  # Chat UI, per-tab session state
    └── README.md
```

## Setup

### 1. Get a free Groq API key
Sign up at [console.groq.com](https://console.groq.com) → API Keys → create a new key.

### 2. Run the backend
```bash
cd backend
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# paste your Groq key into .env
uvicorn main:app --reload --port 8000
```
Verify at `http://localhost:8000/health`.

### 3. Load the extension
1. Open `chrome://extensions`, enable Developer mode
2. "Load unpacked" → select the `extension` folder
3. Pin the extension icon to your toolbar

### 4. Use it
Open any website, click the extension icon, and start asking questions about the page.

## Design decisions worth knowing about

- **Side panel, not a popup that force-opens on every page.** Chrome doesn't allow extensions to auto-launch UI without a user gesture (anti-spam policy). Once you open the panel, it persists across tab navigation, which gets close to "always available" without violating that.
- **Per-tab session isolation.** Early versions had a bug where chatting about a MacBook listing in one tab would get contaminated by content from a different page opened in another tab. Fixed by tracking chat history and page context per `tabId` in the side panel, rather than relying on broadcast messages alone.
- **Page facts vs. general knowledge, clearly separated.** The model is instructed to ground answers in the actual page content first, but can supplement with general knowledge (e.g. "is this good for gaming") when asked — and must explicitly flag when it's doing so, rather than blending the two silently.


## Known limitations (current phase)

- Long pages are truncated to ~12k characters — no chunking yet 
- Chat history lives in memory only — closing the side panel clears it
- CORS is wide open for local development 