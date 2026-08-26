import os
import time
import uuid
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.chat_history import BaseChatMessageHistory, InMemoryChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_core.documents import Document
from langchain_community.embeddings import FastEmbedEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY not set. Copy .env.example to .env and add your key.")

MODEL = "openai/gpt-oss-20b"

llm = ChatGroq(model=MODEL, api_key=GROQ_API_KEY, temperature=0.3, max_tokens=800)

extraction_llm = ChatGroq(model=MODEL, api_key=GROQ_API_KEY, temperature=0, max_tokens=2000)

app = FastAPI(title="Webpage Chat Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*",
    allow_methods=["*"],
    allow_headers=["*"],
)

page_sessions: dict = {}

session_pages: dict = {}

history_store: dict[str, InMemoryChatMessageHistory] = {}


def get_session_history(session_id: str) -> BaseChatMessageHistory:
    if session_id not in history_store:
        history_store[session_id] = InMemoryChatMessageHistory()
    return history_store[session_id]


text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)

vector_stores: dict[str, InMemoryVectorStore] = {}

_embeddings = None


def get_embeddings():
    global _embeddings
    if _embeddings is None:
        _embeddings = FastEmbedEmbeddings()
    return _embeddings


def get_vector_store(session_id: str) -> InMemoryVectorStore:
    if session_id not in vector_stores:
        vector_stores[session_id] = InMemoryVectorStore(get_embeddings())
    return vector_stores[session_id]


SYSTEM_TEMPLATE = (
    "You are a helpful assistant answering questions about webpages the user has "
    "browsed during this session. Below are the most relevant excerpts retrieved "
    "from those pages — they may come from the page open right now, or from other "
    "pages visited earlier in this same session.\n\n"
    "The page currently open is: {current_title} ({current_url}). If the user says "
    "'this page' or doesn't specify, assume they mean the current page.\n\n"
    "All pages visited in this session so far:\n{visited_pages}\n\n"
    "Rules:\n"
    "1. Prioritize facts in the excerpts below — prices, specs, availability, reviews. "
    "Treat these as ground truth over your own knowledge.\n"
    "2. NEVER state a specific number (price, RAM, storage, dimensions, etc.) for a "
    "product unless that exact number appears in the excerpts below. If a number isn't "
    "in the excerpts, say it's not available — do not estimate or recall a typical/likely "
    "value from your own knowledge, even if it seems plausible.\n"
    "3. Non-numeric reasoning (e.g. 'is this good for gaming', 'what are likely pros/cons') "
    "can use general knowledge, but must be clearly marked as inference, e.g. 'Not stated "
    "on the page, but generally:'. Never blend inference with page facts silently.\n"
    "4. NEVER invent or compare against a product/page that isn't listed in 'All pages "
    "visited' above. If the user references something ambiguous ('the previous one', "
    "'that other one') and it's unclear which visited page they mean, ASK which one "
    "rather than guessing.\n"
    "5. When the user says 'current' or 'this one', it means {current_title} — never "
    "confuse it with another visited page, even if that page was discussed more recently "
    "in the conversation.\n"
    "6. Use a table ONLY when the user is directly comparing 2+ distinct items side by "
    "side. For describing a single subject, explaining something, or answering a general "
    "question, use short paragraphs and/or bullet points — never a table. This renders in "
    "a narrow ~320px sidebar, so tables should stay to 3-4 columns with short cell text.\n\n"
    "Relevant excerpts:\n{context}"
)

prompt = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_TEMPLATE),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{question}\n\n[Answer specifically about \"{current_title}\" using the excerpts above. Do not reuse facts from earlier answers in this conversation unless they also appear in the excerpts.]"),
    ]
)

chain = prompt | llm

chain_with_history = RunnableWithMessageHistory(
    chain,
    get_session_history,
    input_messages_key="question",
    history_messages_key="history",
)


CONTEXTUALIZE_TEMPLATE = (
    "Given the conversation so far and the user's latest question, rewrite it as a "
    "standalone question that names the specific product(s)/page(s) being referred to. "
    "Use ONLY products/pages that were actually mentioned earlier in this conversation "
    "or match the current page ({current_title}) — never introduce a product that wasn't "
    "already part of the conversation. If the question is already standalone, return it "
    "unchanged. If it's genuinely ambiguous which earlier page is meant, keep that "
    "ambiguity explicit in your rewrite rather than guessing which one.\n\n"
    "Return ONLY the rewritten question, nothing else — no preamble, no quotes."
)

contextualize_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", CONTEXTUALIZE_TEMPLATE),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{question}"),
    ]
)

contextualize_chain = contextualize_prompt | llm


class ProductSpec(BaseModel):
    label: str = Field(description="Spec name, e.g. 'Processor', 'RAM'")
    value: str = Field(description="Spec value as stated on the page")


class ProductInfo(BaseModel):
    is_product_page: bool = Field(
        description="True if this page is a product/shopping listing. False for articles, "
        "docs, homepages, etc. — if false, leave other fields empty."
    )
    product_name: Optional[str] = None
    price: Optional[str] = Field(default=None, description="Current/offer price as shown on the page")
    original_price: Optional[str] = Field(default=None, description="List/original price before discount, if shown")
    discount: Optional[str] = Field(default=None, description="Discount amount or percentage, if shown")
    rating: Optional[str] = Field(default=None, description="Star rating and/or review count, if shown")
    availability: Optional[str] = Field(default=None, description="In stock / out of stock / delivery info")
    key_specs: List[ProductSpec] = Field(default_factory=list)
    pros: List[str] = Field(default_factory=list, description="Only if explicitly stated or clearly implied by specs")
    cons: List[str] = Field(default_factory=list, description="Only if explicitly stated or clearly implied by specs")


extract_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You extract structured product information from webpage content. "
            "Only use information explicitly present on the page — do not invent prices, "
            "specs, or ratings. If a field isn't present on the page, leave it empty rather "
            "than guessing. If this page is not a product/shopping listing, set "
            "is_product_page to false.",
        ),
        ("human", "Page title: {title}\nPage URL: {url}\n\nPage content:\n{page_content}"),
    ]
)

extract_chain = extract_prompt | extraction_llm.with_structured_output(ProductInfo)


class IngestRequest(BaseModel):
    session_id: Optional[str] = None
    url: str
    title: str
    page_content: str


class ChatRequest(BaseModel):
    session_id: str
    question: str


class ChatResponse(BaseModel):
    answer: str


class ExtractRequest(BaseModel):
    session_id: str


page_store: dict[str, dict[str, dict]] = {}

DIRECT_INCLUDE_CHAR_LIMIT = 6000

SESSION_TTL_SECONDS = 30 * 60
MAX_SESSIONS = 20

last_seen: dict[str, float] = {}


def evict_session(session_id: str) -> None:
    page_sessions.pop(session_id, None)
    session_pages.pop(session_id, None)
    history_store.pop(session_id, None)
    vector_stores.pop(session_id, None)
    page_store.pop(session_id, None)
    last_seen.pop(session_id, None)


def touch_session(session_id: str) -> None:
    last_seen[session_id] = time.time()

    now = time.time()
    stale = [sid for sid, ts in last_seen.items() if now - ts > SESSION_TTL_SECONDS]
    for sid in stale:
        evict_session(sid)

    if len(last_seen) > MAX_SESSIONS:
        oldest = sorted(last_seen.items(), key=lambda kv: kv[1])
        overflow = len(last_seen) - MAX_SESSIONS
        for sid, _ in oldest[:overflow]:
            evict_session(sid)


def get_page_context(session_id: str, url: str, search_query: str) -> Optional[str]:
    stored = page_store.get(session_id, {}).get(url)
    if not stored:
        return None
    if len(stored["content"]) <= DIRECT_INCLUDE_CHAR_LIMIT:
        return stored["content"]
    store = get_vector_store(session_id)
    docs = store.similarity_search(
        search_query, k=4, filter=lambda d, u=url: d.metadata.get("url") == u
    )
    if docs:
        return "\n\n".join(d.page_content for d in docs)
    return stored["content"][:DIRECT_INCLUDE_CHAR_LIMIT]


@app.post("/ingest")
def ingest_page(req: IngestRequest):
    session_id = req.session_id or str(uuid.uuid4())
    touch_session(session_id)

    prev = page_sessions.get(session_id)
    is_new_page = prev is None or prev["url"] != req.url

    page_sessions[session_id] = {
        "url": req.url,
        "title": req.title,
        "page_content": req.page_content,
    }

    page_store.setdefault(session_id, {})[req.url] = {
        "title": req.title,
        "content": req.page_content,
    }

    if is_new_page:
        if len(req.page_content) > DIRECT_INCLUDE_CHAR_LIMIT:
            store = get_vector_store(session_id)
            chunks = text_splitter.split_text(req.page_content)
            docs = [
                Document(page_content=chunk, metadata={"url": req.url, "title": req.title})
                for chunk in chunks
            ]
            if docs:
                store.add_documents(docs)

        pages = session_pages.setdefault(session_id, [])
        if not any(p["url"] == req.url for p in pages):
            pages.append({"url": req.url, "title": req.title})

    return {"session_id": session_id, "status": "ok", "is_new_page": is_new_page}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if req.session_id not in page_sessions:
        raise HTTPException(status_code=404, detail="Session not found. Call /ingest first.")

    touch_session(req.session_id)

    current_page = page_sessions[req.session_id]
    history_messages = get_session_history(req.session_id).messages
    pages_visited = session_pages.get(req.session_id, [])
    current_url = current_page["url"]

    if history_messages:
        rewritten = contextualize_chain.invoke(
            {
                "question": req.question,
                "current_title": current_page["title"],
                "history": history_messages,
            }
        )
        search_query = rewritten.content.strip() or req.question
    else:
        search_query = req.question

    context_parts = []

    current_content = get_page_context(req.session_id, current_url, search_query)
    if current_content:
        context_parts.append(f"[Source: {current_page['title']} — {current_url}]\n{current_content}")

    for p in pages_visited[-8:]:
        if p["url"] == current_url:
            continue
        content = get_page_context(req.session_id, p["url"], search_query)
        if content:
            context_parts.append(f"[Source: {p['title']} — {p['url']}]\n{content}")

    context_block = "\n\n".join(context_parts) if context_parts else current_page["page_content"][:4000]

    visited_pages_block = "\n".join(f"- {p['title']} ({p['url']})" for p in pages_visited) or "- (none yet)"

    result = chain_with_history.invoke(
        {
            "question": req.question,
            "current_title": current_page["title"],
            "current_url": current_page["url"],
            "context": context_block,
            "visited_pages": visited_pages_block,
        },
        config={"configurable": {"session_id": req.session_id}},
    )

    return ChatResponse(answer=result.content)


@app.post("/extract", response_model=ProductInfo)
def extract_product(req: ExtractRequest):
    if req.session_id not in page_sessions:
        raise HTTPException(status_code=404, detail="Session not found. Call /ingest first.")

    touch_session(req.session_id)

    page = page_sessions[req.session_id]

    try:
        result = extract_chain.invoke(
            {
                "title": page["title"],
                "url": page["url"],
                "page_content": page["page_content"][:20000],
            }
        )
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Extraction failed — the model's structured output didn't parse. Try again.",
        )
    return result


@app.get("/health")
def health():
    return {"status": "ok"}