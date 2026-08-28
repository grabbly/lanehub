# Changelog

## Unreleased

- **One login, one page.** The separate `/admin` (password) and `/portal`
  (email+password) entrances are unified into a single sign-in form at the hub
  root `/`. Role is resolved server-side: a **blank email** + `HUB_ADMIN_PASSWORD`
  signs in the superadmin (tabbed panel: Lanes / Team / Feed / Settings); an
  **email** + password signs in a member (their own lane, no menu). One session
  cookie (`hub_session`) now carries both roles; auth moved to `POST /api/login`,
  `POST /api/logout`, `GET /api/session`. `/admin` and `/portal` 307-redirect to
  `/`, so existing invite links keep working, and the data APIs
  (`/admin/api/*`, `/portal/api/*`) are unchanged. Hub settings (project chat +
  SMTP) now live under a dedicated **Settings** tab.

- **Wake a Claude Code session on `@mention`.** When a human writes
  `@<bot_username>` in the chat, the lane's Claude Code session can now resume
  and answer. LaneHub detects the mention server-side and stores, per lane, the
  wake cursor and the current session id, exposed via two new bridge endpoints
  (`GET /{lane}/wake`, `POST /{lane}/wake/ack`). A thin, stateless watcher
  ([scripts/telegram_watch.py](scripts/telegram_watch.py)) polls `/wake`, runs
  `claude --resume <id> -p …` in the lane's project dir, and reports the
  resulting session id back via `/wake/ack`. One watcher can serve many lanes;
  the chat contract (`/feed`, `/send`) is unchanged, so existing agents keep
  working with no edits. The watcher runs from a single stdlib file — env vars
  for one project, a JSON config for several — and offers `--status`,
  `--dry-run`, and `--once` for control and transparency. Wake state (armed,
  cursor, current session id, pending mention) is exposed server-side under
  `wake` in `GET /{lane}/info`, so you can see what a remote watcher is doing
  without shelling into its machine. The hub serves the watcher itself at
  `GET /watcher.py`, and the CLAUDE.md agent-prompt block (admin **agent
  recipes** + member portal) now includes the one-liners to start, check
  (`--status`) and stop it — so an agent can manage its own watcher. See
  [docs/WATCHER.md](docs/WATCHER.md).

## 0.4.1 — 2026-08-04

- **Browsers can save the login password again.** Both the admin and the member
  portal login were a bare `<input>` plus a click handler — no `<form>`, so no
  submit event ever fired and password managers (Safari / iCloud Keychain in
  particular) never offered to save anything. Both are real forms now, with
  `name=` attributes, a submit button and a `submit` handler; the admin screen
  carries a fixed visually-hidden `username` (managers need an account to key
  the entry on, and they skip `display:none` fields). Clearing the password
  field is deferred ~1s so the manager captures the value, not an empty string.
  The manual Enter-key handler is gone — the form does that natively.

## 0.4.0 — 2026-07-26

- **SMTP configurable from the admin panel** (Team → 📧 Email): host, port,
  user, password, from, STARTTLS stored in the hub DB, plus a send-test-email
  button. Panel config overrides `HUB_SMTP_*` env vars; the stored password
  is write-only (API returns a `passwordSet` flag, never the value).

## 0.3.0 — 2026-07-26

- **Member portal + invitations.** The admin sets a hub-wide **project chat**
  and invites teammates by email; each member logs into `/portal` with
  generated credentials, pastes their own BotFather token, and gets a lane
  wired to the project chat plus API key, inlined curl recipes and a
  CLAUDE.md agent-prompt block (RU/EN). Rotate-key and password change
  included. Invitation emails via optional SMTP, otherwise copy-paste texts.
- Send fallback chain: explicit `chatId` → lane default → project chat.
- Session tokens now carry a subject (admin / member email) — sessions issued
  by earlier versions are invalidated, log in again.

## 0.2.x — 2026-07-26

- **Teammate onboarding messages** (RU/EN copy-paste) in the admin UI.
- **Slug optional** on lane creation — auto-derived from the bot username,
  uniquified; the Add-lane form leads with the bot token.

## 0.1.x — 2026-07-26

- First release: multi-lane Telegram bridge with web admin (lanes, generated
  API keys, rotation, merged live feed, send-as-lane), webhook/polling
  delivery, groups + channels, message chunking, media markers, single-SQLite
  storage, Docker/Caddy deployment, fake-Telegram dev server, pytest suite.
- Post-deploy fixes from the first real install: configurable `HUB_PORT`,
  Apache reverse-proxy recipe, cookie diagnostics on login.
