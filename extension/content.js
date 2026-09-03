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

function announcePageLoaded() {
  chrome.runtime.sendMessage({ type: "PAGE_LOADED", ...extractPageContent() });
}

announcePageLoaded();

let lastUrl = window.location.href;

function handlePossibleNavigation() {
  if (window.location.href !== lastUrl) {
    lastUrl = window.location.href;
    setTimeout(announcePageLoaded, 400);
  }
}

const originalPushState = history.pushState;
history.pushState = function (...args) {
  const result = originalPushState.apply(this, args);
  handlePossibleNavigation();
  return result;
};

const originalReplaceState = history.replaceState;
history.replaceState = function (...args) {
  const result = originalReplaceState.apply(this, args);
  handlePossibleNavigation();
  return result;
};

window.addEventListener("popstate", handlePossibleNavigation);

let lastLength = document.body.innerText.length;
const observer = new MutationObserver(() => {
  const newLength = document.body.innerText.length;
  if (Math.abs(newLength - lastLength) > 200) {
    lastLength = newLength;
    announcePageLoaded();
  }
});
observer.observe(document.body, { childList: true, subtree: true });

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "REQUEST_PAGE_CONTENT") {
    sendResponse(extractPageContent());
  }
});