# Webpage Chat Assistant

A Chrome extension that lets you chat with any webpage in real time — ask questions about a product listing, article, or documentation page and get answers grounded in what's actually on the page, powered by an LLM backend.

**Example use case:** you're browsing a product on Amazon or Flipkart and don't want to read the entire listing. Open the side panel and ask "what's the price after offers?" or "what are the cons?" — get a direct answer instead of scanning the page yourself.


## How it works

1. A content script extracts clean text from whatever page you're viewing
2. That content is sent to a FastAPI backend, which holds it as context for the conversation
3. Questions you ask in the side panel are answered by an LLM (via Groq) using that page as grounding
4. Each browser tab keeps its own independent chat session — switching tabs shows that tab's conversation, not a mix of pages
5. As you navigate to different pages within the same tab, each page's content is added to that session's memory — you can ask about a page you looked at earlier without re-opening it

## Tech stack

- **Extension:** Chrome Manifest V3, Side Panel API, vanilla JS
- **Backend:** FastAPI (Python)
- **LLM orchestration:** LangChain (`ChatGroq`, `RunnableWithMessageHistory`, `InMemoryVectorStore`)
- **Embeddings:** FastEmbed (local, free, no API key)
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

## Structured extraction (Phase 3)

Beyond free-form chat, there's a dedicated "Extract Product Info" button that returns
typed, structured data instead of a text answer — price, discount, rating, a spec table,
pros/cons — rendered as a proper card rather than parsed out of markdown.

This uses LangChain's `with_structured_output()` bound to a Pydantic schema (`ProductInfo`),
so the model is constrained to return valid typed data via tool calling, rather than us
regex-parsing a text response. Runs on a separate `ChatGroq` instance with more output
tokens and temperature 0, since structured extraction needs room for full spec lists and
benefits from determinism more than chat does.

## Multi-page memory (Phase 4)

The conversation isn't limited to a single page anymore. As you navigate to different
pages in the same tab, each page's content joins that session's memory instead of
replacing it — you can ask a question about a page you looked at five clicks ago.

Every visited page under ~4000 characters is kept in full and injected directly into
the prompt, source-tagged by page title/URL, so the model always has clean, complete
data per page rather than fragments. Pages longer than that fall back to chunking +
embeddings (`RecursiveCharacterTextSplitter` + FastEmbed + `InMemoryVectorStore`) with
similarity search against the question — real RAG, but only where it's actually needed,
since chunking a short structured page (a product spec table) turned out to fragment
it and cause wrong answers rather than help.

A follow-up question like "how's this better than the previous one?" gets rewritten
into a standalone question using chat history before retrieval runs, since vague
follow-ups barely resemble the actual page content and were causing weak, sometimes
hallucinated retrieval matches.

## Design decisions worth knowing about

- **Side panel, not a popup that force-opens on every page.** Chrome doesn't allow extensions to auto-launch UI without a user gesture (anti-spam policy). Once you open the panel, it persists across tab navigation, which gets close to "always available" without violating that.
- **Per-tab session isolation.** Early versions had a bug where chatting about a MacBook listing in one tab would get contaminated by content from a different page opened in another tab. Fixed by tracking chat history and page context per `tabId` in the side panel, rather than relying on broadcast messages alone.
- **Page facts vs. general knowledge, clearly separated.** The model is instructed to ground answers in the actual page content first, but can supplement with general knowledge (e.g. "is this good for gaming") when asked — and must explicitly flag when it's doing so, rather than blending the two silently.

## Known limitations (current phase)

- Chat history lives in memory only — closing the side panel clears it 
- CORS is wide open for local development — will be scoped down before deployment
- Structured extraction is tuned for shopping/product pages — other page types will mostly return `is_product_page: false`
- Occasionally, when two pages are asked the same/similar question back-to-back, the model leans on its previous answer instead of the new page's context (an LLM recency-bias tendency, not a missing-data bug — verified by testing that full multi-page comparisons across 3+ pages retrieve and attribute data correctly). Mitigated with an explicit per-turn reminder of which page is "current," not fully eliminated.