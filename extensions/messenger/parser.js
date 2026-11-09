// Reads Messenger's page. Everything that depends on messenger.com's markup
// lives here (and in page.js), so a Messenger redesign means fixing these.
//
// What the group page looks like (checked 2026-11):
//   [role=log]                              the message list
//     [data-message-id="mid.$…"]            one message; the id is Messenger's own, stable
//       h4                                  "You sent" / the sender's first name — only on
//                                           the first message of a run from one sender
//       [dir=auto] (nested, duplicated)     the text
//       [role=button]{Play} + [role=timer]  a voice message and its length ("1:45")
//   [role=textbox][contenteditable]         the message box (Lexical editor)
//       img (in the bubble, >= 80 px)      a photo; several form a grid in one message
// A voice clip's URL and a photo's full-size URL are not in the DOM; page.js
// copies them from React's props into data-lanehub-audio / data-lanehub-photos
// on the message.

globalThis.LaneHubParser = (() => {
  function threadId() {
    const m = location.pathname.match(/\/t\/(\d+)/);
    return m ? m[1] : "";
  }

  function threadTitle() {
    return (document.title || "").replace(/^\(\d+\)\s*/, "").replace(/\s*\|\s*Messenger\s*$/, "").trim();
  }

  function messageList() {
    return document.querySelector('[role="main"] [role="log"]');
  }

  // Innermost [dir=auto] blocks outside the sender heading: the text, once
  // (Messenger nests the same text in two dir=auto layers).
  function textOf(el) {
    const leaves = [...el.querySelectorAll('[dir="auto"]')].filter(
      (d) => !d.querySelector('[dir="auto"]') && !d.closest("h1,h2,h3,h4,h5,h6"),
    );
    const parts = [];
    for (const d of leaves) {
      const t = (d.innerText || "").trim();
      if (t && parts[parts.length - 1] !== t) parts.push(t);
    }
    return parts.join("\n");
  }

  // Own messages sit at the right edge of the list; everyone else's start
  // after the avatar column and never reach it (bubbles are narrower than
  // the list). Language-independent, unlike the "You sent" heading.
  function isOwn(el, list) {
    const body =
      [...el.querySelectorAll('[dir="auto"]')].find((d) => !d.closest("h1,h2,h3,h4,h5,h6")) ||
      el.querySelector('[role="slider"]') ||
      el.querySelector("img");
    if (!body) return null;
    const r = body.getBoundingClientRect();
    const lr = list.getBoundingClientRect();
    if (!r.width) return null;
    const left = r.left - lr.left;
    const right = lr.right - r.right;
    return right < 60 && right < left;
  }

  // "At 11:47 AM, Bartłomiej: Hej!" — the English label is a fallback for
  // the sender of a message whose run started above the loaded history.
  function senderFromLabel(el) {
    const m = (el.getAttribute("aria-label") || "").match(/^At [^,]+, ([^:]+?)(?::|$)/);
    return m ? m[1].trim() : "";
  }

  function readMessages(botName = "") {
    const list = messageList();
    if (!list) return [];
    const out = [];
    let lastFrom = "";
    for (const el of list.querySelectorAll("[data-message-id]")) {
      const id = el.dataset.messageId;
      if (!id) continue;
      const own = isOwn(el, list);
      if (own === null) continue; // not laid out yet
      const heading = (el.querySelector("h4")?.innerText || "").trim();
      let from;
      if (own) {
        from = botName || "me";
      } else {
        from = heading || senderFromLabel(el) || lastFrom || "?";
      }
      lastFrom = own ? "" : from;
      const voice = !!el.querySelector('[role="slider"]') && !!el.querySelector('[role="timer"]');
      let text = textOf(el);
      if (heading && text.startsWith(heading)) text = text.slice(heading.length).trim();
      const photos = voice ? [] : photosOf(el);
      // a photo page.js has not got to yet
      const photoPending = !voice && !photos.length && !el.hasAttribute("data-lanehub-photos") && hasBubbleImage(el);
      const photo = photos.length > 0 || photoPending;
      if (!text && !voice && !photo) text = el.querySelector("img:not([alt=''])") ? "[photo]" : "[attachment]";
      out.push({
        id,
        from,
        text,
        outgoing: own,
        kind: voice ? "voice" : photo ? "photo" : "text",
        replyTo: null,
        duration: voice ? durationOf(el) : undefined,
        photos,
        photoPending,
        el,
      });
    }
    return out;
  }

  // [{url, width, height, preview?}] page.js found for the message.
  function photosOf(el) {
    try {
      const list = JSON.parse(el.getAttribute("data-lanehub-photos") || "[]");
      return Array.isArray(list) ? list.filter((p) => typeof p?.url === "string") : [];
    } catch {
      return [];
    }
  }

  // Same test as page.js: a picture in the bubble big enough to be a photo
  // (avatars and emoji are smaller).
  function hasBubbleImage(el) {
    return [...el.querySelectorAll("img")].some((img) => {
      if (img.closest("h1,h2,h3,h4,h5,h6")) return false;
      const r = img.getBoundingClientRect();
      return r.width >= 80 && r.height >= 60;
    });
  }

  function durationOf(el) {
    const t = (el.querySelector('[role="timer"]')?.innerText || "").trim();
    if (!/^\d+(:\d{2}){1,2}$/.test(t)) return undefined;
    return t.split(":").reduce((sum, part) => sum * 60 + Number(part), 0);
  }

  function composer() {
    return document.querySelector('[role="main"] [role="textbox"][contenteditable="true"]');
  }

  // Type text into Messenger's editor the way a paste would, so its own
  // state (the Send button) follows. Leaves the caret at the end.
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

  // {url, duration} of a voice message once page.js has found its clip.
  async function voiceClip(message) {
    const url = message.el?.getAttribute("data-lanehub-audio");
    if (!url) return null;
    return { url, duration: message.duration };
  }

  // The element that scrolls the message list (scrolling it to the top makes
  // Messenger load older messages).
  function scroller() {
    const list = messageList();
    if (!list) return null;
    const scrolls = (el) => {
      const oy = getComputedStyle(el).overflowY;
      return (oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 10;
    };
    for (let el = list; el && el !== document.body; el = el.parentElement) {
      if (scrolls(el)) return el;
    }
    return [...list.querySelectorAll("div")].find(scrolls) || null;
  }

  function messageCount() {
    return messageList()?.querySelectorAll("[data-message-id]").length ?? 0;
  }

  return { threadId, threadTitle, readMessages, composer, typeInto, voiceClip, scroller, messageCount };
})();
