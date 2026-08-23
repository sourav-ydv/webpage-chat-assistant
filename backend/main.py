import os
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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

app = FastAPI(title="Webpage Chat Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

page_sessions: dict = {}

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

chain_with_history = RunnableWithMessageHistory(
    chain,
    get_session_history,
    input_messages_key="question",
    history_messages_key="history",
)


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


@app.get("/health")
def health():
    return {"status": "ok"}