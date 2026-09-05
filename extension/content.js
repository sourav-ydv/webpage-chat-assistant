function extractPageContent() {
  const clone = document.body.cloneNode(true);
  clone.querySelectorAll("script, style, noscript, svg, nav, footer, iframe").forEach((el) => el.remove());
  const text = clone.innerText.replace(/\s+/g, " ").trim();

  return {
    url: window.location.href,
    title: document.title,
    page_content: text,
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "REQUEST_PAGE_CONTENT") {
    sendResponse(extractPageContent());
  }
});