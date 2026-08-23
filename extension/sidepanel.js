const BACKEND_URL = "http://localhost:8000";

const chatLog = document.getElementById("chat-log");
const pageTitleEl = document.getElementById("page-title");
const input = document.getElementById("question-input");
const sendBtn = document.getElementById("send-btn");
const extractBtn = document.getElementById("extract-btn");

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

function appendMessageDom(text, sender, htmlOverride) {
  const div = document.createElement("div");
  div.className = `msg ${sender}`;
  if (htmlOverride) {
    div.innerHTML = htmlOverride;
  } else if (sender === "bot") {
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

function addStructuredMessage(tabId, html) {
  const session = getOrCreateSession(tabId);
  session.messages.push({ html, sender: "bot" });
  if (tabId === activeTabId) {
    appendMessageDom(null, "bot", html);
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
  session.messages.forEach((m) => appendMessageDom(m.text, m.sender, m.html));
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

async function requestPageContent(tabId) {
  try {
    return await chrome.tabs.sendMessage(tabId, { type: "REQUEST_PAGE_CONTENT" });
  } catch (err) {
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
      return await chrome.tabs.sendMessage(tabId, { type: "REQUEST_PAGE_CONTENT" });
    } catch (injectErr) {
      return null;
    }
  }
}

async function switchToTab(tabId) {
  activeTabId = tabId;
  renderActiveTab();

  const pageData = await requestPageContent(tabId);
  if (pageData) {
    await ingestPage(tabId, pageData);
  } else if (!tabSessions[tabId]) {
    pageTitleEl.textContent = "Can't read this page";
  }
}

function renderProductCard(data) {
  if (!data.is_product_page) {
    return `<p>This doesn't look like a product page — nothing to extract here.</p>`;
  }

  let html = `<div class="product-card">`;
  html += `<h4>${escapeHtml(data.product_name || "Unknown product")}</h4>`;

  if (data.price || data.original_price || data.discount) {
    html += `<div class="price-row">`;
    if (data.price) html += `<span class="price">${escapeHtml(data.price)}</span>`;
    if (data.original_price && data.original_price !== data.price) {
      html += `<span class="original-price">${escapeHtml(data.original_price)}</span>`;
    }
    if (data.discount) html += `<span class="discount">${escapeHtml(data.discount)}</span>`;
    html += `</div>`;
  }

  if (data.rating) html += `<div class="rating">⭐ ${escapeHtml(data.rating)}</div>`;
  if (data.availability) html += `<div class="availability">${escapeHtml(data.availability)}</div>`;

  if (data.key_specs && data.key_specs.length) {
    html += `<table class="spec-table">`;
    data.key_specs.forEach((s) => {
      html += `<tr><td>${escapeHtml(s.label)}</td><td>${escapeHtml(s.value)}</td></tr>`;
    });
    html += `</table>`;
  }

  if (data.pros && data.pros.length) {
    html += `<div class="pros"><strong>Pros</strong><ul>`;
    html += data.pros.map((p) => `<li>${escapeHtml(p)}</li>`).join("");
    html += `</ul></div>`;
  }

  if (data.cons && data.cons.length) {
    html += `<div class="cons"><strong>Cons</strong><ul>`;
    html += data.cons.map((c) => `<li>${escapeHtml(c)}</li>`).join("");
    html += `</ul></div>`;
  }

  html += `</div>`;
  return html;
}

async function extractProductInfo() {
  const tabId = activeTabId;
  const session = tabSessions[tabId];
  if (!session || !session.sessionId) {
    addMessage(tabId, "Still loading page content, try again in a second.", "bot");
    return;
  }

  extractBtn.disabled = true;
  extractBtn.textContent = "Extracting...";

  try {
    const res = await fetch(`${BACKEND_URL}/extract`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: session.sessionId }),
    });
    const data = await res.json();
    addStructuredMessage(tabId, renderProductCard(data));
  } catch (err) {
    addMessage(tabId, "Couldn't extract product info. Is the backend running?", "bot");
  } finally {
    extractBtn.disabled = false;
    extractBtn.textContent = "Extract Product Info";
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

extractBtn.addEventListener("click", extractProductInfo);

chrome.tabs.onActivated.addListener(({ tabId }) => {
  switchToTab(tabId);
});

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  if (tabs[0]) switchToTab(tabs[0].id);
});