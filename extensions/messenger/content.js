// Runs on messenger.com. On the lane's group page it reads the messages into
// the hub and types the hub's drafts into the message box — it never presses
// Send: a person does.

(() => {
  const api = globalThis.browser ?? globalThis.chrome;
  const P = globalThis.LaneHubParser;

  const SCAN_DEBOUNCE_MS = 800;
  // A photo message waits this long for page.js to find its pictures before
  // it goes to the hub without them (messages below it wait too, to keep
  // the chat's order).
  const PHOTO_WAIT_MS = 8000;
  const PHOTO_TRIES = 3;

  // page.js runs in the page's own context to find voice clip URLs (React
  // props are invisible from here).
  const pageScript = document.createElement("script");
  pageScript.src = api.runtime.getURL("page.js");
  pageScript.onload = () => pageScript.remove();
  (document.head || document.documentElement).appendChild(pageScript);
  const DRAFT_POLL_MS = 8000;

  let lane = null; // /messenger/status of the configured lane
  let observer = null;
  let scanTimer = null;
  let draftTimer = null;
  const sent = new Map(); // message id -> text last pushed to the hub
  const voiceDone = new Set(); // message ids whose clip went up
  const photoFiles = new Map(); // message id -> [{fileId, width, height}] on the hub
  const photoTries = new Map(); // message id -> failed uploads
  const firstSeen = new Map(); // message id -> when a scan first saw it
  const isLive = new Map(); // message id -> new at the bottom (true) or history (false)
  let known = []; // ids in page order after the last scan

  function call(type, extra = {}) {
    return new Promise((resolve) => {
      api.runtime.sendMessage({ type, ...extra }, (res) => {
        resolve(res ?? { ok: false, error: api.runtime.lastError?.message ?? "no response" });
      });
    });
  }

  // --- the small status pill (only this browser sees it) -----------------

  let pill = null;
  function status(text, tone = "ok") {
    if (!pill) {
      pill = document.createElement("div");
      pill.style.cssText =
        "position:fixed;left:12px;bottom:12px;z-index:2147483647;padding:4px 10px;border-radius:12px;" +
        "font:12px -apple-system,BlinkMacSystemFont,sans-serif;color:#fff;pointer-events:none;opacity:.85";
      document.documentElement.appendChild(pill);
    }
    pill.textContent = `LaneHub · ${text}`;
    pill.style.background = { ok: "#2f7d32", warn: "#b26a00", err: "#b3261e" }[tone] ?? "#555";
    api.storage.local.set({ lastStatus: { text, tone, at: Date.now() } });
  }

  // Only the lane's own group is ever read or typed into. Checked again
  // before every step, because Messenger switches chats without a page load.
  function onLaneThread() {
    return !!lane && P.threadId() === String(lane.defaultChatId);
  }

  // --- reading -------------------------------------------------------------

  async function scan() {
    if (!onLaneThread()) return stop();
    const all = P.readMessages(lane.botUsername);
    if (!all.length) return;
    // A message is live when it appears below every message already known
    // (new at the bottom); anything above that is history scrolled into view.
    // The first scan of a page is all history. Decided when a message is
    // first seen, even if it waits for its photos and goes up later.
    const seen = new Set(known);
    let lastKnown = -1;
    all.forEach((m, i) => { if (seen.has(m.id)) lastKnown = i; });
    all.forEach((m, i) => {
      if (!isLive.has(m.id)) isLive.set(m.id, lastKnown >= 0 && i > lastKnown && !seen.has(m.id));
    });
    known = all.map((m) => m.id);
    const msgs = await withPhotos(all);
    const live = [];
    const backfill = [];
    for (const m of msgs) {
      if (sent.get(m.id) === bodyOf(m)) continue;
      (isLive.get(m.id) ? live : backfill).push(m);
    }
    for (const [batch, isBackfill] of [[backfill, true], [live, false]]) {
      if (!batch.length) continue;
      const res = await call("ingest", {
        payload: {
          chatId: P.threadId(),
          chatTitle: P.threadTitle(),
          backfill: isBackfill,
          messages: batch.map(({ id, from, text, outgoing, kind, replyTo, fileRefs }) => ({
            id, from, text, outgoing, kind, replyTo, ...(fileRefs?.length ? { photos: fileRefs } : {}),
          })),
        },
      });
      if (!res.ok) {
        status(res.error, "err");
        return;
      }
      for (const m of batch) sent.set(m.id, bodyOf(m));
    }
    await uploadVoices(msgs);
    status(`reading · ${all.length} on screen`);
  }

  // What was last pushed for a message; a change (an edit, photos that
  // finally went up) sends it again.
  function bodyOf(m) {
    return `${m.from}\u0000${m.text}\u0000${(m.fileRefs || []).map((f) => f.fileId).join(",")}`;
  }

  // Upload each photo message's pictures before the message itself goes to
  // the hub, so its feed row has them from the start. Returns the messages
  // that are ready, in page order: a photo message whose pictures page.js
  // has not found yet holds back itself and everything below it, for up to
  // PHOTO_WAIT_MS.
  async function withPhotos(msgs) {
    const now = Date.now();
    const ready = [];
    let retry = false;
    for (const m of msgs) {
      if (!firstSeen.has(m.id)) firstSeen.set(m.id, now);
      if (m.kind !== "photo") {
        ready.push(m);
        continue;
      }
      const waited = now - firstSeen.get(m.id);
      // no pictures found yet, or only on-screen previews: give page.js time
      // to find the full-size ones
      if ((m.photoPending || m.photos.some((p) => p.preview)) && waited < PHOTO_WAIT_MS) {
        retry = true;
        break;
      }
      if (!onLaneThread()) return [];
      let refs = photoFiles.get(m.id);
      const tries = photoTries.get(m.id) || 0;
      const wanted = m.photos.length;
      if (wanted && (!refs || refs.length < wanted) && tries < PHOTO_TRIES) {
        const got = [];
        for (const p of m.photos) {
          let res;
          try {
            res = await call("photo", { chatId: P.threadId(), ...(await photoSource(p)) });
          } catch (e) {
            res = { ok: false, error: String(e?.message ?? e) };
          }
          if (!res.ok) {
            photoTries.set(m.id, tries + 1);
            status(`photo: ${res.error}`, "warn");
            retry = true;
            break;
          }
          got.push({ fileId: res.data.fileId, width: p.width || undefined, height: p.height || undefined });
          if (p.preview) status("photo: only a preview was found on the page", "warn");
        }
        if (got.length === wanted) refs = got;
        if (refs) photoFiles.set(m.id, refs);
      }
      if (!refs && tries < PHOTO_TRIES && wanted && waited < PHOTO_WAIT_MS) {
        retry = true;
        break; // try again shortly, keeping the order
      }
      ready.push({ ...m, fileRefs: refs || [] });
    }
    if (retry) {
      clearTimeout(photoTimer);
      photoTimer = setTimeout(scheduleScan, 2000);
    }
    return ready;
  }
  let photoTimer = null;

  // A blob: URL (an end-to-end encrypted chat) only opens in this page, so
  // its bytes travel to the background as base64; https links are fetched
  // there.
  async function photoSource(p) {
    if (!p.url.startsWith("blob:")) return { url: p.url };
    const blob = await (await fetch(p.url)).blob();
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return { base64: btoa(bin), mime: blob.type };
  }

  async function uploadVoices(msgs) {
    for (const m of msgs) {
      if (!onLaneThread()) return;
      if (m.kind !== "voice" || voiceDone.has(m.id)) continue;
      const clip = await P.voiceClip(m);
      if (!clip) continue; // its audio isn't on the page yet
      const res = await call("voice", { messageId: m.id, chatId: P.threadId(), ...clip });
      if (res.ok) voiceDone.add(m.id);
      else status(`voice: ${res.error}`, "warn");
    }
  }

  // One scan at a time: the timer and the history walk below both start them.
  let scanning = Promise.resolve();
  function runScan() {
    scanning = scanning.then(scan).catch((e) => status(String(e), "err"));
    return scanning;
  }

  function scheduleScan() {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(runScan, SCAN_DEBOUNCE_MS);
  }

  // --- photos stored without their files -----------------------------------
  //
  // The hub counts photo messages of the last two weeks that it has no file
  // for (read before 0.2, or when a picture could not be taken). On opening
  // the group the extension scrolls its history up, a screen at a time, so
  // those messages are read again with their photos, until the hub's count
  // is 0, the top of the chat is reached, or OLD_PHOTO_STEPS. Then it
  // scrolls back down. Not repeated within a day while the count is the same
  // (a message the hub took for a photo may have been something else).

  const OLD_PHOTO_STEPS = 60;
  const OLD_PHOTO_PAUSE_MS = 2500;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let walking = false;

  async function fetchOldPhotos(force = false) {
    const missing = lane?.photosMissing?.count ?? 0;
    if (walking || !missing || !onLaneThread()) return;
    const key = `oldPhotos:${lane.lane}`;
    const last = (await api.storage.local.get(key))[key];
    if (!force && last && last.count === missing && Date.now() - last.at < 24 * 3600 * 1000) return;
    walking = true;
    let left = missing;
    try {
      let unchanged = 0;
      let count = P.messageCount();
      for (let step = 0; step < OLD_PHOTO_STEPS && left > 0 && onLaneThread(); step++) {
        status(`fetching older photos · ${left} left`, "warn");
        const box = P.scroller();
        if (!box) break;
        // already at the top sets off no scroll event: step down a little first
        if (box.scrollTop < 5) {
          box.scrollTop = 40;
          await sleep(100);
        }
        box.scrollTop = 0;
        box.dispatchEvent(new Event("scroll"));
        await sleep(OLD_PHOTO_PAUSE_MS);
        await runScan();
        const now = P.messageCount();
        unchanged = now === count ? unchanged + 1 : 0;
        count = now;
        if (unchanged >= 3) {
          // the top of the chat: let the photos still waiting go up
          await sleep(PHOTO_WAIT_MS);
          await runScan();
        }
        const res = await call("status");
        if (res.ok) {
          lane = res.data;
          left = lane.photosMissing?.count ?? 0;
        }
        if (unchanged >= 3) break;
      }
      const box = P.scroller();
      if (box) box.scrollTop = box.scrollHeight;
      status(left ? `older photos: ${left} not found` : "older photos fetched", left ? "warn" : "ok");
    } finally {
      walking = false;
      api.storage.local.set({ [key]: { count: left, at: Date.now() } });
    }
  }

  // --- drafts --------------------------------------------------------------

  async function pollDrafts() {
    const res = await call("drafts");
    if (!res.ok) return;
    const next = res.data.drafts.find((d) => d.status === "pending");
    if (!next) return;
    if (!onLaneThread()) return stop();
    const box = P.composer();
    if (!box) return;
    if ((box.innerText || "").trim()) {
      status("draft waiting — clear the message box", "warn");
      return; // never type over what a person is writing
    }
    if (!P.typeInto(box, next.text)) {
      status("could not type the draft", "err");
      return;
    }
    await call("draft", { id: next.id, status: "filled" });
    status("draft in the box — press Enter to send", "warn");
  }

  // --- lifecycle -----------------------------------------------------------

  function stop() {
    observer?.disconnect();
    observer = null;
    clearInterval(draftTimer);
    draftTimer = null;
    clearTimeout(scanTimer);
    scanTimer = null;
    clearTimeout(photoTimer);
    photoTimer = null;
    known = [];
    isLive.clear();
  }

  async function start() {
    stop();
    const res = await call("status");
    if (!res.ok) {
      status(res.error, "err");
      return;
    }
    lane = res.data;
    if (!onLaneThread()) {
      if (pill) pill.remove(), (pill = null);
      return; // another chat: stay quiet
    }
    observer = new MutationObserver(scheduleScan);
    observer.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ["data-lanehub-photos", "data-lanehub-audio"],
    });
    draftTimer = setInterval(() => pollDrafts().catch(() => {}), DRAFT_POLL_MS);
    scheduleScan();
    setTimeout(() => fetchOldPhotos().catch((e) => status(String(e), "err")), 4000);
  }

  // Messenger is a single-page app: follow thread switches.
  let lastPath = "";
  setInterval(() => {
    if (location.pathname !== lastPath) {
      lastPath = location.pathname;
      start();
    }
  }, 1500);
})();
