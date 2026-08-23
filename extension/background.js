chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "PAGE_LOADED" && sender.tab) {
    chrome.runtime.sendMessage({ ...message, tabId: sender.tab.id }).catch(() => {
    });
  }
  return true;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.runtime.sendMessage({ type: "TAB_CLOSED", tabId }).catch(() => {});
});