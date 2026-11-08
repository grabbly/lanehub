// Reads Messenger's page. Everything that depends on messenger.com's markup
// lives here, so a Messenger redesign means fixing this one file.
//
// Draft: selectors still to be checked against the live group page.

globalThis.LaneHubParser = (() => {
  function threadId() {
    const m = location.pathname.match(/\/t\/(\d+)/);
    return m ? m[1] : "";
  }

  function threadTitle() {
    return (document.title || "").replace(/^\(\d+\)\s*/, "").replace(/\s*[|·]\s*Messenger$/, "").trim();
  }

  function readMessages() {
    return [];
  }

  function composer() {
    return document.querySelector('[role="main"] [contenteditable="true"][role="textbox"]');
  }

  // Type text into Messenger's editor the way a paste would, so its own
  // state (the Send button, drafts) follows. Leaves the caret at the end.
  function typeInto(box, text) {
    box.focus();
    const range = document.createRange();
    range.selectNodeContents(box);
    range.collapse(false);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand("insertText", false, text);
    return (box.innerText || "").trim().length > 0;
  }

  async function voiceClip(_message) {
    return null;
  }

  return { threadId, threadTitle, readMessages, composer, typeInto, voiceClip };
})();
