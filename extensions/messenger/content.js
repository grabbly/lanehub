// Runs on messenger.com. On the lane's group page it reads the messages into
// the hub and types the hub's drafts into the message box — it never presses
// Send: a person does.

(() => {
  const api = globalThis.browser ?? globalThis.chrome;
  const P = globalThis.LaneHubParser;

  const SCAN_DEBOUNCE_MS = 800;

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
    const msgs = P.readMessages(lane.botUsername);
    if (!msgs.length) return;
    // A message is live when it appears below every message already known
    // (new at the bottom); anything above that is history scrolled into view.
    // The first scan of a page is all history.
    const seen = new Set(known);
    let lastKnown = -1;
    msgs.forEach((m, i) => { if (seen.has(m.id)) lastKnown = i; });
    const live = [];
    const backfill = [];
    msgs.forEach((m, i) => {
      const body = `${m.from}\u0000${m.text}`;
      if (sent.get(m.id) === body) return;
      (lastKnown >= 0 && i > lastKnown && !seen.has(m.id) ? live : backfill).push(m);
    });
    known = msgs.map((m) => m.id);
    for (const [batch, isBackfill] of [[backfill, true], [live, false]]) {
      if (!batch.length) continue;
      const res = await call("ingest", {
        payload: {
          chatId: P.threadId(),
          chatTitle: P.threadTitle(),
          backfill: isBackfill,
          messages: batch.map(({ id, from, text, outgoing, kind, replyTo }) => ({ id, from, text, outgoing, kind, replyTo })),
        },
      });
      if (!res.ok) {
        status(res.error, "err");
        return;
      }
      for (const m of batch) sent.set(m.id, `${m.from}\u0000${m.text}`);
    }
    await uploadVoices(msgs);
    status(`reading · ${msgs.length} on screen`);
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

  function scheduleScan() {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(() => scan().catch((e) => status(String(e), "err")), SCAN_DEBOUNCE_MS);
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
    known = [];
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
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    draftTimer = setInterval(() => pollDrafts().catch(() => {}), DRAFT_POLL_MS);
    scheduleScan();
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
