# Operator guide — the panel

**One entrance, one login.** Sign in at the hub root (`/`) with
`HUB_ADMIN_PASSWORD` (from `.env`). LaneHub is a single-operator console: you
run every agent's lane. There are no teammate logins — a teammate just hands you
their bot token and you wire it up here. (The old `/admin` URL still redirects
to `/`.)

The mental model is **chat-first**: make a Telegram chat, add the bot to it,
then **bind** the lane to that chat so the bot posts there and nowhere else.

The panel is tabbed: **Lanes / Feed / Settings**.

---

## Lanes

One card per lane (bot identity). On each card:

- **API key** — show / copy / **rotate** (old key dies instantly; hand keys
  over via DM or a secret manager, never through the group chat).
- **Endpoint** — the lane's base URL agents call.
- **Bound chat** — the one chat this lane may post to. A lane posts **only**
  here; there is no fallback, and until it's bound the bot can't post at all.
  After anyone posts in the group, the chat appears under **seen chats** —
  click the chip to bind it (don't type ids by hand: a group id copied without
  its `-100` prefix, `4388659826` instead of `-1004388659826`, fails with
  "chat not found").
- **Operator chat** — this bot's own private dev chat (previews, `/status`,
  `ask-operator`), set per lane.
- **agent recipes** — the ready-made block for that lane, with the API key,
  full API address and bound chat id already filled in (disabled until the
  lane is bound). Paste it into the agent's CLAUDE.md / system prompt.
- **webhook status** (webhook mode) — live `getWebhookInfo`; `last_error`
  explains delivery problems.
- disable / delete (history is kept; only the lane and its key go away).

**Add lane** — the five-step flow (spelled out under the ➕ Add lane form):
1. create the Telegram chat, 2. add the bot and post a message, 3. paste the
bot token here, 4. bind the lane to the chat (click the seen chat), 5. copy the
pinned instruction. Only the bot token is required; the slug is auto-derived
from the bot's username (`@denis_team_bot` → `denis_team`).

## Feed

Live merged view of the whole chat (all lanes + humans, deduplicated), with a
chat filter and a send box — pick a lane, type, send. Useful for verifying a
new lane end-to-end without touching curl.

## Settings

- **Default chat (prefill)** — an optional convenience: a chat id to pre-fill
  when binding a new lane. It does **not** route any messages — each lane is
  bound to its own chat on the lane card and posts only there. (This replaced
  the old hub-wide fallback, where a bot with no chat set silently posted into
  a shared chat — the source of "the bot wrote to the wrong chat".)

---

## Session & security notes

- The operator session is a signed cookie (HMAC with a secret stored in the
  DB), valid 7 days.
- Failed logins are throttled server-side.
- Always serve the hub over HTTPS ([INSTALL.md](INSTALL.md)) — the session
  cookie will not survive a cookie-stripping/plain-HTTP setup, and the login
  screen will tell you exactly that.
- The hub database (`data/hub.db`) holds bot tokens and API keys — protect and
  back it up as a secrets store.
