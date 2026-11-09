// Runs in messenger.com's own page context (injected by content.js), where
// React's props are visible. Attachment URLs live only there, so this copies
// them onto the message element for the content script to read:
//   data-lanehub-audio   a voice message's clip (props.playableUrl)
//   data-lanehub-photos  JSON [{url, width, height, preview?}] of the
//                        message's photos, full size where the props have it
// It reads nothing else and sends nothing anywhere.
//
// Debugging a Messenger redesign: in the page's console,
//   lanehubPhotoDebug($0)   (with a message selected in the Elements panel)
// lists the attachment-like props found under that element.

(() => {
  if (window.__lanehubAudio) return;
  window.__lanehubAudio = true;

  const FIBER_LIMIT = 3000;

  function fiberOf(el) {
    const key = Object.keys(el).find((k) => k.startsWith("__reactFiber"));
    return key ? el[key] : null;
  }

  // Every memoizedProps object under the element, in document order.
  function* propsUnder(el) {
    const root = fiberOf(el);
    if (!root) return;
    const stack = [root.child];
    let visited = 0;
    while (stack.length && visited < FIBER_LIMIT) {
      const fiber = stack.pop();
      if (!fiber) continue;
      visited++;
      if (fiber.memoizedProps && typeof fiber.memoizedProps === "object") yield fiber.memoizedProps;
      stack.push(fiber.sibling, fiber.child);
    }
  }

  function attachmentsIn(props) {
    const out = [];
    if (props.attachment && typeof props.attachment === "object") out.push(props.attachment);
    if (Array.isArray(props.attachments)) out.push(...props.attachments.filter((a) => a && typeof a === "object"));
    return out;
  }

  // --- voice ---------------------------------------------------------------

  // The audio player and the attachment row below the message carry it as
  // props.attachment.playableUrl / props.attachments[i].playableUrl.
  function playableUrl(el) {
    for (const props of propsUnder(el)) {
      for (const a of attachmentsIn(props)) {
        if (typeof a.playableUrl === "string" && a.playableUrl.startsWith("https://")) return a.playableUrl;
      }
    }
    return "";
  }

  // --- photos --------------------------------------------------------------

  const isUrl = (u) => typeof u === "string" && /^(https:\/\/|blob:https:\/\/www\.messenger\.com\/)/.test(u);
  const num = (n) => (Number.isFinite(n) && n > 0 ? Math.round(n) : 0);

  // An attachment row names its renditions in one of two styles: flat
  // (largePreviewUrl + largePreviewWidth, previewUrl…) or nested GraphQL
  // ({large_preview: {uri, width, height}}, original_dimensions). Largest wins.
  const FLAT = ["largePreview", "largeImage", "image", "preview", "thumbnail"];
  const NESTED = ["large_preview", "largePreview", "large_image", "largeImage", "image", "preview", "thumbnail"];

  function renditions(a) {
    const out = [];
    for (const base of FLAT) {
      const url = a[`${base}Url`] ?? a[`${base}Uri`];
      const fallback = a[`${base}UrlFallback`];
      const w = num(a[`${base}Width`]);
      const h = num(a[`${base}Height`]);
      if (isUrl(url)) out.push({ url, width: w, height: h });
      else if (isUrl(fallback)) out.push({ url: fallback, width: w, height: h });
    }
    for (const base of NESTED) {
      const o = a[base];
      if (o && typeof o === "object" && isUrl(o.uri ?? o.url)) {
        out.push({ url: o.uri ?? o.url, width: num(o.width), height: num(o.height) });
      }
    }
    const dims = a.original_dimensions ?? a.originalDimensions;
    if (dims && out.length && !out.some((r) => r.width)) {
      out[0].width = num(dims.x ?? dims.width);
      out[0].height = num(dims.y ?? dims.height);
    }
    return out;
  }

  function isPhoto(a) {
    if (a.playableUrl) return false; // voice, audio, video
    const type = String(a.attachmentType ?? a.__typename ?? a.mimeType ?? a.attachmentMimeType ?? "").toLowerCase();
    if (/sticker|video|audio|file|share|xma/.test(type) && !/image|photo/.test(type)) return false;
    if (a.stickerId || a.stickerFbid || a.sticker_id) return false;
    return true;
  }

  function photoKey(a, url) {
    const id = a.attachmentFbid ?? a.legacy_attachment_id ?? a.attachmentId ?? a.fbid ?? a.id;
    return id ? `id:${id}` : `url:${url.split("?")[0]}`;
  }

  // {photos, other}: the message's photos, and whether it also holds
  // attachments that are not photos (a video's thumbnail is no photo).
  function photosFromProps(el) {
    const found = new Map();
    let other = false;
    for (const props of propsUnder(el)) {
      for (const a of attachmentsIn(props)) {
        if (!isPhoto(a)) {
          other = true;
          continue;
        }
        const best = renditions(a).sort((x, y) => y.width * y.height - x.width * x.height)[0];
        if (!best) continue;
        const key = photoKey(a, best.url);
        const had = found.get(key);
        if (!had || had.width * had.height < best.width * best.height) found.set(key, best);
      }
    }
    return { photos: [...found.values()], other };
  }

  // Pictures in the bubble itself: avatars and emoji are small; reactions sit
  // in buttons. Used to tell a photo message apart, and as the last resort
  // (a preview) when the props carry no URL.
  function bubbleImages(el) {
    return [...el.querySelectorAll("img")].filter((img) => {
      if (img.closest('h1,h2,h3,h4,h5,h6,[role="button"] [role="img"]')) return false;
      const r = img.getBoundingClientRect();
      return r.width >= 80 && r.height >= 60 && isUrl(img.currentSrc || img.src);
    });
  }

  function photosOf(el) {
    const shown = bubbleImages(el);
    if (!shown.length) return null;
    const { photos: full, other } = photosFromProps(el);
    if (full.length >= shown.length || other) return full;
    // the props had fewer: keep those and add what is only on screen
    const out = [...full];
    for (const img of shown.slice(full.length)) {
      out.push({ url: img.currentSrc || img.src, width: img.naturalWidth || 0, height: img.naturalHeight || 0, preview: true });
    }
    return out;
  }

  // --- tagging -------------------------------------------------------------

  function tag() {
    for (const el of document.querySelectorAll('[role="log"] [data-message-id]')) {
      if (!el.hasAttribute("data-lanehub-audio") && el.querySelector('[role="slider"]')) {
        const url = playableUrl(el);
        if (url) el.setAttribute("data-lanehub-audio", url);
      }
      if (el.querySelector('[role="slider"]')) continue;
      const had = el.getAttribute("data-lanehub-photos");
      if (had === "[]") continue; // a video or another attachment, no photo
      const hadList = had ? JSON.parse(had) : [];
      const shownCount = bubbleImages(el).length;
      // re-read while more tiles appear or a preview may turn into a full image
      if (had && hadList.length >= shownCount && !hadList.some((p) => p.preview)) continue;
      // [] when the props show attachments but none is a photo
      const photos = photosOf(el);
      if (!photos) continue;
      const json = JSON.stringify(photos);
      if (json !== had) el.setAttribute("data-lanehub-photos", json);
    }
  }

  window.lanehubPhotoDebug = (el) => {
    const out = [];
    for (const props of propsUnder(el.closest?.("[data-message-id]") || el)) {
      for (const a of attachmentsIn(props)) out.push({ keys: Object.keys(a), photo: isPhoto(a), renditions: renditions(a) });
    }
    return out;
  };

  let timer = null;
  new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(tag, 500);
  }).observe(document.body, { childList: true, subtree: true });
  tag();
})();
