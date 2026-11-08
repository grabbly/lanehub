// Runs in messenger.com's own page context (injected by content.js), where
// React's props are visible. A voice message's clip URL lives only there
// (props.playableUrl), so this copies it into data-lanehub-audio on the
// message for the content script to read. It reads nothing else and sends
// nothing anywhere.

(() => {
  if (window.__lanehubAudio) return;
  window.__lanehubAudio = true;

  // The audio player and the attachment row below the message carry it as
  // props.attachment.playableUrl / props.attachments[i].playableUrl.
  function urlIn(props) {
    const atts = [props?.attachment, ...(Array.isArray(props?.attachments) ? props.attachments : [])];
    for (const a of atts) {
      if (typeof a?.playableUrl === "string" && a.playableUrl.startsWith("https://")) return a.playableUrl;
    }
    return "";
  }

  function playableUrl(el) {
    const key = Object.keys(el).find((k) => k.startsWith("__reactFiber"));
    if (!key) return "";
    const stack = [el[key].child];
    let visited = 0;
    while (stack.length && visited < 1500) {
      const fiber = stack.pop();
      if (!fiber) continue;
      visited++;
      const url = urlIn(fiber.memoizedProps);
      if (url) return url;
      stack.push(fiber.sibling, fiber.child);
    }
    return "";
  }

  function tag() {
    for (const el of document.querySelectorAll('[role="log"] [data-message-id]')) {
      if (el.hasAttribute("data-lanehub-audio") || !el.querySelector('[role="slider"]')) continue;
      const url = playableUrl(el);
      if (url) el.setAttribute("data-lanehub-audio", url);
    }
  }

  let timer = null;
  new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(tag, 500);
  }).observe(document.body, { childList: true, subtree: true });
  tag();
})();
