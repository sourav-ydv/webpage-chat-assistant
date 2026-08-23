const BACKEND_URL = "http://localhost:8000";

const chatLog = document.getElementById("chat-log");
const pageTitleEl = document.getElementById("page-title");
const input = document.getElementById("question-input");
const sendBtn = document.getElementById("send-btn");

let activeTabId = null;

const tabSessions = {};

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function formatMarkdown(raw) {
  let text = escapeHtml(raw);
  text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");

  const lines = text.split("\n");
  let html = "";
  let listType = null;

  const closeList = () => {
    if (listType) { html += `</${listType}>`; listType = null; }
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    const ulMatch = line.match(/^[-*]\s+(.*)/);
    const olMatch = line.match(/^\d+[.)]\s+(.*)/);

    if (ulMatch) {
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${ulMatch[1]}</li>`;
    } else if (olMatch) {
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += `<li>${olMatch[1]}</li>`;
    } else {
      closeList();
      if (line !== "") html += `<p>${line}</p>`;
    }
  }
  closeList();
  return html;
}

function appendMessageDom(text, sender) {
  const div = document.createElement("div");
  div.className = `msg ${sender}`;
  if (sender === "bot") {
    div.innerHTML = formatMarkdown(text);
  } else {
    div.textContent = text;
  }
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function getOrCreateSession(tabId) {
  if (!tabSessions[tabId]) {
    tabSessions[tabId] = { sessionId: null, url: null, title: null, messages: [] };
  }
  return tabSessions[tabId];
}

function addMessage(tabId, text, sender) {
  const session = getOrCreateSession(tabId);
  session.messages.push({ text, sender });
  if (tabId === activeTabId) {
    appendMessageDom(text, sender);
  }
}

function renderActiveTab() {
  chatLog.innerHTML = "";
  const session = tabSessions[activeTabId];
  if (!session) {
    pageTitleEl.textContent = "Loading page...";
    return;
  }
  pageTitleEl.textContent = session.title || session.url || "Loading page...";
  session.messages.forEach((m) => appendMessageDom(m.text, m.sender));
}

async function ingestPage(tabId, pageData) {
  const session = getOrCreateSession(tabId);
  const isNewPage = session.url !== pageData.url;

  session.url = pageData.url;
  session.title = pageData.title;

  const res = await fetch(`${BACKEND_URL}/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: session.sessionId,
      url: pageData.url,
      title: pageData.title,
      page_content: pageData.page_content,
    }),
  });
  const data = await res.json();
  session.sessionId = data.session_id;

  if (tabId === activeTabId) {
    pageTitleEl.textContent = pageData.title || pageData.url;
  }

  if (isNewPage) {
    session.messages = [{ text: `Ready. Ask me anything about "${pageData.title}".`, sender: "bot" }];
    if (tabId === activeTabId) {
      chatLog.innerHTML = "";
      appendMessageDom(session.messages[0].text, "bot");
    }
  }
}

async function askQuestion(question) {
  const tabId = activeTabId;
  const session = tabSessions[tabId];
  if (!session || !session.sessionId) {
    addMessage(tabId, "Still loading page content, try again in a second.", "bot");
    return;
  }
  addMessage(tabId, question, "user");
  input.value = "";

  try {
    const res = await fetch(`${BACKEND_URL}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: session.sessionId, question }),
    });
    const data = await res.json();
    addMessage(tabId, data.answer, "bot");
  } catch (err) {
    addMessage(tabId, "Error reaching backend. Is it running?", "bot");
  }
}

async function switchToTab(tabId) {
  activeTabId = tabId;
  renderActiveTab();

  try {
    const pageData = await chrome.tabs.sendMessage(tabId, { type: "REQUEST_PAGE_CONTENT" });
    if (pageData) await ingestPage(tabId, pageData);
  } catch (err) {
    if (!tabSessions[tabId]) {
      pageTitleEl.textContent = "Can't read this page";
    }
  }
}

sendBtn.addEventListener("click", () => {
  const q = input.value.trim();
  if (q) askQuestion(q);
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const q = input.value.trim();
    if (q) askQuestion(q);
  }
});


chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "PAGE_LOADED" && message.tabId != null) {
    ingestPage(message.tabId, message);
  }
  if (message.type === "TAB_CLOSED" && message.tabId != null) {
    delete tabSessions[message.tabId];
  }
});

chrome.tabs.onActivated.addListener(({ tabId }) => {
  switchToTab(tabId);
});

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  if (tabs[0]) switchToTab(tabs[0].id);
});