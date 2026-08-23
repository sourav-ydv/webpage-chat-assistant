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

# --- Page metadata per session (which page is this session talking about) ---
# { session_id: {"url":, "title":, "page_content":} }
page_sessions: dict = {}

# --- LangChain-managed chat history, one per session ---
history_store: dict[str, InMemoryChatMessageHistory] = {}


def get_session_history(session_id: str) -> BaseChatMessageHistory:
    """RunnableWithMessageHistory calls this to fetch/create a session's message history."""
    if session_id not in history_store:
        history_store[session_id] = InMemoryChatMessageHistory()
    return history_store[session_id]


SYSTEM_TEMPLATE = (
    "You are a helpful assistant answering questions about the webpage the user is "
    "currently viewing. You are shown the page content below.\n\n"
    "Rules:\n"
    "1. Prioritize facts stated on the page — prices, specs, availability, reviews. "
    "Treat these as ground truth over your own knowledge.\n"
    "2. If the user asks something not explicitly stated on the page but you can "
    "reasonably reason about it using general knowledge (e.g. 'is this good for "
    "gaming', 'what are likely cons', 'how does this compare to competitors'), "
    "answer using your own knowledge — but clearly mark that part as inference, "
    "e.g. start with 'Not stated on the page, but generally:' or similar.\n"
    "3. Never blend the two silently. The user must always be able to tell what "
    "came from the page vs. what came from your general knowledge.\n"
    "4. This response renders in a narrow chat sidebar (~320px wide). Do NOT use "
    "markdown tables — they don't fit. Use short paragraphs and bullet points "
    "(lines starting with '-') instead. Keep answers tight, no filler.\n\n"
    "Page title: {title}\nPage URL: {url}\n\nPage content:\n{page_content}"
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
        "page_content": req.page_content[:12000],
    }

    # New page in this tab -> old conversation no longer applies to it, start clean.
    if is_new_page and session_id in history_store:
        history_store[session_id].clear()

    return {"session_id": session_id, "status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if req.session_id not in page_sessions:
        raise HTTPException(status_code=404, detail="Session not found. Call /ingest first.")

    page = page_sessions[req.session_id]

    result = chain_with_history.invoke(
        {
            "question": req.question,
            "title": page["title"],
            "url": page["url"],
            "page_content": page["page_content"],
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
                "page_content": page["page_content"],
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