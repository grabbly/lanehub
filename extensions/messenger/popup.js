const api = globalThis.browser ?? globalThis.chrome;

api.storage.local.get(["lastStatus", "lane"]).then(({ lastStatus, lane }) => {
  const el = document.getElementById("state");
  if (!lane) {
    el.textContent = "Not configured yet — open Settings.";
  } else if (lastStatus) {
    const ago = Math.round((Date.now() - lastStatus.at) / 1000);
    el.textContent = `Lane ${lane}: ${lastStatus.text} (${ago}s ago)`;
  }
});

document.getElementById("settings").addEventListener("click", (e) => {
  e.preventDefault();
  api.runtime.openOptionsPage();
});
