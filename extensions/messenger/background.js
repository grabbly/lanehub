// Talks to the hub for the content script. Running the requests here (with
// the hub's origin granted as a host permission) keeps them out of
// messenger.com's page: no CORS, and the lane key never enters the page.

const api = globalThis.browser ?? globalThis.chrome;

async function config() {
  const { hubUrl = "", lane = "", key = "" } = await api.storage.local.get(["hubUrl", "lane", "key"]);
  if (!hubUrl || !lane || !key) throw new Error("not configured: open the extension's settings");
  return { hubUrl: hubUrl.replace(/\/+$/, ""), lane, key };
}

async function hub(path, { method = "GET", json, form } = {}) {
  const { hubUrl, lane, key } = await config();
  const headers = { "X-Bridge-Token": key };
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }
  const resp = await fetch(`${hubUrl}/${lane}${path}`, { method, headers, body });
  const text = await resp.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text.slice(0, 200) };
  }
  if (!resp.ok) throw new Error(`hub ${resp.status}: ${data.detail ?? "error"}`);
  return data;
}

function fromBase64(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function uploadVoice({ messageId, chatId, url, base64, mime, duration }) {
  let blob;
  if (base64) {
    blob = new Blob([fromBase64(base64)], { type: mime || "audio/mp4" });
  } else {
    const resp = await fetch(url); // signed CDN link, no cookies needed
    if (!resp.ok) throw new Error(`voice clip download failed: HTTP ${resp.status}`);
    blob = await resp.blob();
  }
  const form = new FormData();
  form.append("file", blob, "voice" + (/(mpeg|mp3)/.test(blob.type) ? ".mp3" : ".mp4"));
  form.append("messageId", messageId);
  form.append("chatId", chatId);
  if (Number.isFinite(duration)) form.append("duration", String(Math.round(duration)));
  return hub("/messenger/voice", { method: "POST", form });
}

const PHOTO_MAX = 25 * 1024 * 1024;

// A photo from the thread, full size: fetched from Messenger's CDN (a signed
// link, no cookies needed) or handed over as base64 by the content script.
// The hub answers with its fileId, which the message then lists.
async function uploadPhoto({ chatId, url, base64, mime }) {
  let blob;
  if (base64) {
    blob = new Blob([fromBase64(base64)], { type: mime || "image/jpeg" });
  } else {
    if (!/^https:\/\/[^/]+\.(fbcdn\.net|fbsbx\.com)\//.test(url || "")) throw new Error("photo link is not Messenger's");
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`photo download failed: HTTP ${resp.status}`);
    blob = await resp.blob();
  }
  if (!blob.size) throw new Error("photo is empty");
  if (blob.size > PHOTO_MAX) throw new Error("photo is larger than 25 MB");
  const ext = { "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp" }[blob.type] ?? ".jpg";
  const form = new FormData();
  form.append("file", blob, "photo" + ext);
  form.append("chatId", chatId);
  return hub("/messenger/photo", { method: "POST", form });
}

const handlers = {
  status: () => hub("/messenger/status"),
  ingest: ({ payload }) => hub("/messenger/ingest", { method: "POST", json: payload }),
  drafts: () => hub("/messenger/drafts"),
  draft: ({ id, status }) => hub(`/messenger/drafts/${id}`, { method: "POST", json: { status } }),
  voice: (msg) => uploadVoice(msg),
  photo: (msg) => uploadPhoto(msg),
};

api.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const handler = handlers[msg?.type];
  if (!handler) return false;
  handler(msg).then(
    (data) => sendResponse({ ok: true, data }),
    (err) => sendResponse({ ok: false, error: String(err?.message ?? err) }),
  );
  return true; // answer asynchronously
});
