const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);

function show(text, ok) {
  const el = $("result");
  el.textContent = text;
  el.className = ok ? "ok" : "err";
}

async function load() {
  const cfg = await api.storage.local.get(["hubUrl", "lane", "key"]);
  $("hubUrl").value = cfg.hubUrl ?? "";
  $("lane").value = cfg.lane ?? "";
  $("key").value = cfg.key ?? "";
}

async function grantHub(hubUrl) {
  // The hub's origin is an optional host permission: ask for it on Save (a
  // click, as the browser requires). Safari may manage it in its own settings.
  const origin = new URL(hubUrl).origin + "/*";
  try {
    if (api.permissions?.request && !(await api.permissions.contains({ origins: [origin] }))) {
      await api.permissions.request({ origins: [origin] });
    }
  } catch {
    // not supported here — the request itself will tell if access is missing
  }
}

$("save").addEventListener("click", async () => {
  const hubUrl = $("hubUrl").value.trim().replace(/\/+$/, "");
  const lane = $("lane").value.trim().toLowerCase();
  const key = $("key").value.trim();
  if (!/^https?:\/\//.test(hubUrl) || !lane || !key) {
    show("Fill in all three fields.", false);
    return;
  }
  await grantHub(hubUrl);
  await api.storage.local.set({ hubUrl, lane, key });
  const res = await api.runtime.sendMessage({ type: "status" });
  if (!res?.ok) {
    show(`Saved, but the hub did not answer: ${res?.error ?? "no response"}`, false);
    return;
  }
  const s = res.data;
  show(
    `Connected to lane "${s.lane}".\n` +
      `Bot account: ${s.botUsername}\n` +
      (s.threadUrl ? `Group: ${s.threadUrl}\nOpen that page in this browser, signed in as the bot account.` :
        "The lane is not bound to a Messenger group yet — set it in the admin panel."),
    true,
  );
});

load();
