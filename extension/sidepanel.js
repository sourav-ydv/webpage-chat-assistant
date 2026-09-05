const BACKEND_URL = "https://webpage-chat-backend.onrender.com";

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

  const pipeCount = (l) => (l.match(/\|/g) || []).length;
  const isTableRow = (l) => pipeCount(l) >= 2;
  const splitRow = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const isSeparatorRow = (l) => {
    const cells = splitRow(l);
    return cells.length > 0 && cells.every((c) => /^:?-{2,}:?$/.test(c));
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i].trim();

    if (isTableRow(line) && i + 1 < lines.length && isSeparatorRow(lines[i + 1])) {
      closeList();
      const headerCells = splitRow(line);
      html += `<table class="chat-table"><thead><tr>`;
      headerCells.forEach((c) => { html += `<th>${c}</th>`; });
      html += `</tr></thead><tbody>`;
      i += 2;
      while (i < lines.length && isTableRow(lines[i].trim())) {
        const rowCells = splitRow(lines[i].trim());
        html += `<tr>`;
        rowCells.forEach((c) => { html += `<td>${c}</td>`; });
        html += `</tr>`;
        i += 1;
      }
      html += `</tbody></table>`;
      continue;
    }

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
    i += 1;
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

  if (session.ingesting) {
    input.disabled = true;
    sendBtn.disabled = true;
    sendBtn.textContent = "Loading page...";
  } else {
    input.disabled = false;
    sendBtn.disabled = false;
    sendBtn.textContent = "Send";
  }
}

async function fetchWithTimeout(url, options, timeoutMs = 45000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function ingestPage(tabId, pageData) {
  const session = getOrCreateSession(tabId);
  const isNewPage = session.url !== pageData.url;

  session.url = pageData.url;
  session.title = pageData.title;

  if (isNewPage) {
    session.ingesting = true;
    if (tabId === activeTabId) {
      input.disabled = true;
      sendBtn.disabled = true;
      sendBtn.textContent = "Loading page...";
    }
  }

  try {
    const res = await fetchWithTimeout(`${BACKEND_URL}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: session.sessionId,
        url: pageData.url,
        title: pageData.title,
        page_content: pageData.page_content,
      }),
    });

    if (!res.ok) {
      throw new Error(`ingest failed: ${res.status}`);
    }

    const data = await res.json();
    session.sessionId = data.session_id;

    if (isNewPage) {
      if (session.messages.length === 0) {
        addMessage(tabId, `Ready. Ask me anything about "${pageData.title}".`, "bot");
      } else {
        addMessage(tabId, `Added "${pageData.title}" to this conversation's memory — ask about it anytime.`, "bot");
      }
    }
  } catch (err) {
    if (isNewPage) {
      const msg = err.name === "AbortError"
        ? "The backend took too long to respond and the request timed out. Try again."
        : "Couldn't reach the backend to load this page. It may be waking up from sleep — try again in a few seconds.";
      addMessage(tabId, msg, "bot");
    }
  } finally {
    session.ingesting = false;
    if (tabId === activeTabId) {
      pageTitleEl.textContent = pageData.title || pageData.url;
      input.disabled = false;
      sendBtn.disabled = false;
      sendBtn.textContent = "Send";
    }
  }
}

async function askQuestion(question) {
  const tabId = activeTabId;
  const session = tabSessions[tabId];
  if (!session || !session.sessionId || session.ingesting) {
    addMessage(tabId, "Still loading page content, try again in a second.", "bot");
    return;
  }
  addMessage(tabId, question, "user");
  input.value = "";

  try {
    const res = await fetchWithTimeout(`${BACKEND_URL}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: session.sessionId, question }),
    });
    const data = await res.json();
    if (!res.ok) {
      addMessage(tabId, data.detail || "Something went wrong. Try again.", "bot");
      return;
    }
    addMessage(tabId, data.answer, "bot");
  } catch (err) {
    const msg = err.name === "AbortError"
      ? "The backend took too long to respond and the request timed out. Try again."
      : "Couldn't reach the backend. It may be waking up from sleep — wait a few seconds and try again.";
    addMessage(tabId, msg, "bot");
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
    const res = await fetchWithTimeout(`${BACKEND_URL}/extract`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: session.sessionId }),
    });
    if (!res.ok) {
      addMessage(tabId, "Extraction failed on the server — try again in a moment.", "bot");
      return;
    }
    const data = await res.json();
    addStructuredMessage(tabId, renderProductCard(data));
  } catch (err) {
    const msg = err.name === "AbortError"
      ? "The backend took too long to respond and the request timed out. Try again."
      : "Couldn't reach the backend. It may be waking up from sleep — wait a few seconds and try again.";
    addMessage(tabId, msg, "bot");
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
  if (message.type === "TAB_CLOSED" && message.tabId != null) {
    delete tabSessions[message.tabId];
  }
});

extractBtn.addEventListener("click", extractProductInfo);

chrome.tabs.onActivated.addListener(({ tabId }) => {
  switchToTab(tabId);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (!changeInfo.url) return;
  const session = tabSessions[tabId];
  if (session && session.url === changeInfo.url) return;
  setTimeout(async () => {
    const pageData = await requestPageContent(tabId);
    if (pageData) {
      await ingestPage(tabId, pageData);
    }
  }, 600);
});

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  if (tabs[0]) switchToTab(tabs[0].id);
});