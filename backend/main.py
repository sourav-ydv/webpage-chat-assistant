import os
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

# Separate instance for structured extraction: more tokens (full spec lists need room),
# temperature 0 (deterministic — we want consistent structured data, not creative variation).
extraction_llm = ChatGroq(model=MODEL, api_key=GROQ_API_KEY, temperature=0, max_tokens=2000)

app = FastAPI(title="Webpage Chat Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Page metadata per session (which page is "current" for this tab) ---
# { session_id: {"url":, "title":, "page_content":} }
page_sessions: dict = {}

# --- LangChain-managed chat history, one per session ---
history_store: dict[str, InMemoryChatMessageHistory] = {}


def get_session_history(session_id: str) -> BaseChatMessageHistory:
    """RunnableWithMessageHistory calls this to fetch/create a session's message history."""
    if session_id not in history_store:
        history_store[session_id] = InMemoryChatMessageHistory()
    return history_store[session_id]


# --- Phase 4: multi-page memory ---
# Local, free, no API key — first run downloads a small ONNX embedding model (~130MB)
# from Hugging Face and caches it locally.
embeddings = FastEmbedEmbeddings()

text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)

# One vector store per session (per tab), accumulating chunks from every page
# visited in that tab — this is what lets the conversation span multiple pages.
vector_stores: dict[str, InMemoryVectorStore] = {}


def get_vector_store(session_id: str) -> InMemoryVectorStore:
    if session_id not in vector_stores:
        vector_stores[session_id] = InMemoryVectorStore(embeddings)
    return vector_stores[session_id]


SYSTEM_TEMPLATE = (
    "You are a helpful assistant answering questions about webpages the user has "
    "browsed during this session. Below are the most relevant excerpts retrieved "
    "from those pages — they may come from the page open right now, or from other "
    "pages visited earlier in this same session.\n\n"
    "The page currently open is: {current_title} ({current_url}). If the user says "
    "'this page' or doesn't specify, assume they mean the current page. If they ask "
    "about something from earlier browsing, use the excerpts from those other pages.\n\n"
    "Rules:\n"
    "1. Prioritize facts in the excerpts below — prices, specs, availability, reviews. "
    "Treat these as ground truth over your own knowledge.\n"
    "2. If the user asks something not covered in the excerpts but you can reasonably "
    "reason about it using general knowledge, answer using your own knowledge — but "
    "clearly mark that part as inference, e.g. start with 'Not stated on the page, but "
    "generally:' or similar. Never blend the two silently.\n"
    "3. NEVER invent or compare against a product/page that isn't in the excerpts below "
    "or wasn't explicitly discussed earlier in this conversation. If the user references "
    "something ambiguous ('the previous one', 'that other one') and you can't tell which "
    "page they mean from the excerpts or history, ASK which page they mean rather than "
    "guessing or fabricating one.\n"
    "4. This response renders in a narrow chat sidebar (~320px wide). Short paragraphs "
    "and bullet points work best. If comparing 3+ items across several attributes, a "
    "compact markdown table is fine (it renders properly here) — keep cell text short "
    "and prefer 3-4 columns max so it stays readable.\n\n"
    "Relevant excerpts:\n{context}"
)

prompt = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_TEMPLATE),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{question}"),
    ]
)

chain = prompt | llm

# Wraps the chain so LangChain automatically loads/saves message history per session_id,
# instead of us manually appending to a list like before.
chain_with_history = RunnableWithMessageHistory(
    chain,
    get_session_history,
    input_messages_key="question",
    history_messages_key="history",
)


# --- Query rewriting before retrieval ---
# Vague follow-ups ("how's this better than the previous one?") barely resemble the
# actual page chunks in the vector store, so raw similarity search on them retrieves
# weak matches — which is exactly when the model tends to fill the gap by hallucinating.
# Standard fix: rewrite the follow-up into a self-contained question using chat history
# BEFORE searching, so retrieval has something real to match against.
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


# --- Phase 3: structured extraction ---
# Instead of free-form chat, this forces the model to return typed data we can
# render as a proper UI card, rather than parsing markdown out of a text reply.

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


@app.post("/ingest")
def ingest_page(req: IngestRequest):
    session_id = req.session_id or str(uuid.uuid4())

    prev = page_sessions.get(session_id)
    is_new_page = prev is None or prev["url"] != req.url

    page_sessions[session_id] = {
        "url": req.url,
        "title": req.title,
        # No more hard truncation here — chunking below handles length, and the
        # extraction endpoint applies its own generous cap separately.
        "page_content": req.page_content,
    }

    if is_new_page:
        store = get_vector_store(session_id)
        chunks = text_splitter.split_text(req.page_content)
        docs = [
            Document(page_content=chunk, metadata={"url": req.url, "title": req.title})
            for chunk in chunks
        ]
        if docs:
            store.add_documents(docs)
        # Chat history is intentionally NOT cleared here anymore (unlike Phase 1-3).
        # Phase 4's whole point is letting the conversation span multiple pages on a site.

    return {"session_id": session_id, "status": "ok", "is_new_page": is_new_page}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if req.session_id not in page_sessions:
        raise HTTPException(status_code=404, detail="Session not found. Call /ingest first.")

    current_page = page_sessions[req.session_id]
    store = get_vector_store(req.session_id)
    history_messages = get_session_history(req.session_id).messages

    # Rewrite vague follow-ups into a standalone question before searching, so retrieval
    # has real content to match against instead of "this one" / "the previous one".
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

    # Always guarantee some current-page chunks are present, regardless of how the
    # (possibly cross-page) search query happens to score against them — "this page"
    # should never come up empty just because the query was about something else too.
    current_url = current_page["url"]
    current_page_docs = store.similarity_search(
        search_query, k=3, filter=lambda doc: doc.metadata.get("url") == current_url
    )
    general_docs = store.similarity_search(search_query, k=6)

    seen = set()
    relevant_docs = []
    for doc in current_page_docs + general_docs:
        key = (doc.metadata.get("url"), doc.page_content[:80])
        if key not in seen:
            seen.add(key)
            relevant_docs.append(doc)

    if relevant_docs:
        context_block = "\n\n".join(
            f"[Source: {d.metadata.get('title', 'Unknown page')} — {d.metadata.get('url', '')}]\n{d.page_content}"
            for d in relevant_docs
        )
    else:
        # Fallback for the rare case nothing's indexed yet (e.g. ingest hasn't finished)
        context_block = current_page["page_content"][:4000]

    result = chain_with_history.invoke(
        {
            "question": req.question,
            "current_title": current_page["title"],
            "current_url": current_page["url"],
            "context": context_block,
        },
        config={"configurable": {"session_id": req.session_id}},
    )

    return ChatResponse(answer=result.content)


@app.post("/extract", response_model=ProductInfo)
def extract_product(req: ExtractRequest):
    if req.session_id not in page_sessions:
        raise HTTPException(status_code=404, detail="Session not found. Call /ingest first.")

    page = page_sessions[req.session_id]

    try:
        result = extract_chain.invoke(
            {
                "title": page["title"],
                "url": page["url"],
                "page_content": page["page_content"][:20000],  # generous cap for extraction input
            }
        )
    except Exception:
        # Model output got truncated/malformed — surface a clean error instead of a raw 500
        raise HTTPException(
            status_code=502,
            detail="Extraction failed — the model's structured output didn't parse. Try again.",
        )
    return result


@app.get("/health")
def health():
    return {"status": "ok"}