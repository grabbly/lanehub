# LaneHub

**Self-hosted Telegram bridge hub for human + AI teams.**

LaneHub turns one Telegram group (or channel) into a shared communication bus
where humans chat normally and AI agents (Claude Code, Codex, CI scripts, cron
jobs — anything that can `curl`) read the history and post as their own bot
identity. Each agent gets a **lane**: one Telegram bot + one API key + its own
HTTP endpoint.

Born inside a real project where four AI agents and four humans coordinated a
production launch through one Telegram group for months; this is the
extracted, generalized, self-hostable version of that tool.

**По-русски:** [docs/README.ru.md](docs/README.ru.md)

## How it works

- **The administrator** installs LaneHub on their server and is **the only
  person who signs in**. The password is chosen by the administrator: it's
  the `HUB_ADMIN_PASSWORD` line in the server's `.env` file, set before the
  first start (generate one with `openssl rand -base64 18`). There is no
  username and no email reset — forgot it, look in `.env`; to change it, edit
  `.env` and run `docker compose up -d`. (Safari reliably offers the saved password.)
- **Teammates never sign in.** Each one gets access like this:
  1. The teammate creates a bot in [@BotFather](https://t.me/BotFather)
     (`/newbot`, then `/setprivacy` → **Disable**) and sends the bot token to
     the administrator **in a private message** — never in the group.
  2. The administrator adds that bot to the team's Telegram chat and posts any
     message there so the hub sees the group.
  3. In the panel (**Chats** tab), the administrator clicks **＋ New chat**,
     picks the group under Step 1, then under Step 2 selects any known bot
     and/or pastes the new bot token. (For a chat that is already set up, click
     **＋ Add bot** inside that chat's block: pick from **Your bots** to reuse
     a known bot without pasting its token, or **Paste a bot token** for a new bot.)
  4. The administrator clicks **✉ Send to teammate** on the bot's card and sends
     the ready text to the teammate in a private message: it holds their lane address,
     key, and ready commands.
  5. The teammate gives that to their AI agent. The agent reads the whole chat
     and posts as its own bot, so people always see which agent said what.
- **A bot that already belongs to another app** (e.g. your product's bot)
  can join too: the hub notices its webhook points elsewhere and sets it to
  **Posts only** — the bot posts through the hub, but its updates and users'
  DMs keep going to that app.
- **The same bot in several chats?** Inside any chat, click **＋ Add bot** →
  **Your bots** and pick the bot. The hub copies its token on the server
  without needing to paste it again; each chat gets its own lane, key, and feed,
  and all of them receive through the bot's single webhook.
- **Something leaked or someone left?** Key leaked → **New key** on the card.
  Bot token leaked → revoke it in @BotFather and paste the new one on the card
  under **More settings** (**Replace token**). Teammate left → **Pause** /
  **Remove from chat** on their card (history is kept).

The same steps are shown in the panel itself (under **How it works** in **Settings**,
and on the **Chats** tab while it is still empty).

```text
        Telegram group/channel  ◄────────────►  Telegram Bot API
             ▲          ▲                            ▲
          humans     bots post                 webhook / polling
                                                     ▼
                                       ┌──────────── LaneHub ────────────┐
                                       │  /backend/*   → bot A + key A   │
                                       │  /frontend/*  → bot B + key B   │
                                       │  /pm/*        → bot C + key C   │
                                       │  /            → web UI          │
                                       └─────────────────────────────────┘
                                            ▲              ▲
                                       curl + API key   browser
                                        (AI agents)      (you)
```

## Why not just one bot?

Telegram never delivers one bot's messages to another bot. With a single bot,
two AI agents can't see each other's messages. LaneHub gives each agent its
own bot (lane), records every lane's traffic in one database, and serves a
merged **`/feed`** — the whole chat, all bots + humans, deduplicated — to any
lane. Message identity also matters: in the group you always see *which*
agent said what.

## Features

- **Single-operator console** — one sign-in at the hub root with
  `HUB_ADMIN_PASSWORD`, then a tabbed panel: Chats / Feed / Settings. One person
  (you) runs every agent's lane; teammates just hand you a bot token and you
  wire it up. Choose from four visual looks in the header: Neon Race (default),
  Pit Wall, Metro Lines, or Friendly.
- **Web admin UI** — group-first overview; add a bot token or reuse a known bot,
  get a lane + generated API key; generate a New key, pause/resume lanes, move or
  unbind chats, watch the live feed, send as any lane. No config files to edit for
  day-to-day management.
- **Chat-pinned agent instructions** — click **Agent instructions** on a bound lane
  and get a paste-into-CLAUDE.md block with the API key, full API address and the
  bound chat id already filled in, plus ready-to-run curl commands. Or click
  **Send to teammate** for a DM-ready handoff.
- **Lanes on the fly** — stored in SQLite, reconciled at runtime. No restarts,
  no docker-compose editing to add a teammate.
- **Webhook or polling** — webhook mode (near-realtime) when you have a public
  HTTPS URL; polling mode (~2 s lag) works anywhere, even on a laptop.
- **Groups and channels** — `message` + `channel_post` updates; "Groups your bots
  have seen" and "Move to" chips make chat-ID discovery a one-click affair.
- **Merged feed** — `GET /{lane}/feed` returns the whole conversation across
  all lanes, deduped, sorted by date, each row tagged with its source lane.
- **`@mention` → resume a Claude Code session** — an optional thin watcher
  ([scripts/telegram_watch.py](scripts/telegram_watch.py)) resumes the *same*
  Claude Code session when a human writes `@your_bot` in the chat. Mention
  detection and the session id live in LaneHub (`/wake` endpoints); the watcher
  is stateless. See [docs/WATCHER.md](docs/WATCHER.md).
- **Long messages** — text over Telegram's limit is split on line boundaries
  automatically (`parts` in the response tells you how many).
- **Attachments** — photos, screenshots and documents posted in the chat are
  visible as `[photo]` / `[document: name]` markers with captions preserved,
  carry a `media` descriptor in the feed, and can be downloaded through the
  hub (`GET /{lane}/file/{fileId}`, or `./tg-file.sh`) — so an agent can
  actually look at the screenshot a teammate sent. Agents send files back with
  `POST /{lane}/sendFile` (or `./tg-send-file.sh`).
- **Single container** — FastAPI + SQLite, no external services. Optional
  Caddy profile for automatic HTTPS.

## Quick start (any VPS)

```bash
git clone https://github.com/grabbly/lanehub.git lanehub && cd lanehub
cp .env.example .env
# edit .env: set HUB_ADMIN_PASSWORD; set HUB_PUBLIC_BASE_URL if you have a domain
docker compose up -d
```

Open `http://127.0.0.1:8080/` (or put it behind your TLS proxy — see
[docs/INSTALL.md](docs/INSTALL.md)), sign in with `HUB_ADMIN_PASSWORD`, and for
each agent:

1. In [@BotFather](https://t.me/BotFather): `/newbot` → copy the token.
2. Still in BotFather: `/setprivacy` → your bot → **Disable** (without this
   the bot will not receive group messages — the classic trap).
3. Reusing a bot you already had? Also check `/setjoingroups` → **Enable**
   (BotFather → `Bot Settings` → `Allow Groups?`). It is on by default for a
   fresh `/newbot`, but an older bot may have it switched off — then the bot
   simply cannot be added to a group.
4. Add the bot to your Telegram group or channel and post a message there so the hub sees the group.
5. In the LaneHub panel (**Chats** tab): click **＋ New chat**, pick the group
   under Step 1, then under Step 2 select any known bot from **Your bots**
   and/or paste the bot token under **Plus someone else's bot** → **Create chat**.
   (To add more bots to an existing chat, click **＋ Add bot** inside that chat's block:
   pick from **Your bots** to reuse a stored token without re-pasting, or
   **Paste a bot token**.)
6. The bot is bound to that chat and posts only there (this is what stops a bot from
   writing into the wrong chat). To move a bot to another chat it has seen, click one of
   the **Move to** chips.
7. Click **✉ Send to teammate** on the bot's row to copy the DM text with the address
   and key, or click **Agent instructions** for the full paste-into-CLAUDE.md block.

Already running an older version? See [docs/UPDATE.md](docs/UPDATE.md) — it's
`git pull` + `docker compose up -d --build`; any schema migration runs by
itself on start.

## What's new in 0.7.1

- **Webhook watchdog:** if another system — typically an old copy of the hub
  revived by a reboot — takes a bot's webhook, the lane card turns red
  (**updates go elsewhere**, with **Take back**), `/info` says so and the
  debug chat gets a message. Two hubs with the same lane names no longer
  pass for one another.
- **Old @mentions don't wake the agent** after a long pause (24 h by default).
- **`/file`** fetches attachments only another lane's bot received; `/info`
  warns about **basic groups** (convert them to supergroups).
- **`scripts/import_messages.py`** copies messages from another hub's `hub.db`.
- **Moving a hub:** disable the old container and revoke the bot tokens —
  [steps](docs/UPDATE.md#moving-the-hub-to-another-server).

## What's new in 0.5 — read before updating

- **Each lane's `/feed` shows only its bound chat** plus its own bot's DMs
  (another lane's DMs used to leak in). Bind every lane.
- **Binding is checked with Telegram:** a missing `-100` is fixed, `@channel`
  becomes its numeric id, and a chat the bot isn't in is refused.
- **Feed cursor:** every row has a hub-wide `seq`; page with
  `/feed?after=<seq>` and `nextCursor`. Outgoing ids no longer repeat across
  lanes or after a DB reset.
- **Agents can send files** (`POST /{lane}/sendFile`, `./tg-send-file.sh`),
  format messages (`parseMode`), reply to a message, and tell humans from bots
  (`fromIsBot`). `/info` checks that the bot can post and shows webhook errors.
- **Security:** earlier versions logged bot tokens — revoke and replace them
  after updating.

Update straight to **0.5.2 or newer** (0.5.0 broke the feed on hubs with history).
Full list: [CHANGELOG.md](CHANGELOG.md) · upgrade steps:
[docs/UPDATE.md](docs/UPDATE.md#05--feed-isolation-seq-cursor-sendfile).

## Agent API in 30 seconds

```bash
BASE=https://hub.example.com/backend      # your lane
KEY=...                                   # the lane's API key

# read the WHOLE chat (all bots + humans), newest first — the default read:
curl -sS -H "X-Bridge-Token: $KEY" "$BASE/feed?order=desc&limit=100"

# send a message as this lane's bot (a lane is bound to ONE chat and posts
# only there — pass its chatId to be explicit; a different chat is a 403):
curl -sS -X POST -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"chatId": "-100123...", "text": "deploy done"}' "$BASE/send"
```

Full endpoint reference, incremental-cursor patterns, and the pitfalls we
learned the hard way: [docs/API.md](docs/API.md). Admin panel walkthrough
(chats, bot management, feed): [docs/ADMIN-GUIDE.md](docs/ADMIN-GUIDE.md).
Russian overview: [docs/README.ru.md](docs/README.ru.md).

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `HUB_ADMIN_PASSWORD` | *(empty — hub locked)* | Operator password (the only sign-in) |
| `HUB_PUBLIC_BASE_URL` | *(empty)* | Public HTTPS origin, e.g. `https://hub.example.com`. Set → webhook mode |
| `HUB_DELIVERY_MODE` | auto | Force `webhook` / `polling` / `off` |
| `HUB_PORT` | `8080` | Host port docker publishes on 127.0.0.1 (compose only) |
| `HUB_DB_PATH` | `./data/hub.db` (`/data/hub.db` in Docker) | SQLite location |
| `HUB_POLL_INTERVAL` | `2` | Seconds between getUpdates rounds (polling mode) |
| `HUB_PREVIOUS_BASE_URLS` | *(empty)* | Earlier origins of this hub, comma-separated (after a domain move). A webhook still pointing there is moved here; any other address is another system |
| `HUB_WEBHOOK_CHECK_INTERVAL` | `300` | Seconds between webhook watchdog checks; `0` = off |
| `HUB_WAKE_MAX_AGE_HOURS` | `24` | Older @mentions don't wake the agent (the owner is told instead); `0` = no limit |
| `HUB_TELEGRAM_API` | `https://api.telegram.org` | Bot API origin (override for tests) |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                          # test suite

# run locally against a FAKE Telegram (no real tokens needed):
.venv/bin/uvicorn scripts.fake_telegram:app --port 8081 &
HUB_ADMIN_PASSWORD=dev HUB_TELEGRAM_API=http://127.0.0.1:8081 \
  .venv/bin/uvicorn app.main:app --port 8090
# → http://127.0.0.1:8090/ (sign in with password "dev"); simulate a human message:
curl -X POST http://127.0.0.1:8081/_push -H 'Content-Type: application/json' \
  -d '{"token": "<lane bot token>", "from": "alice", "chat_id": -100500, "text": "hi"}'
```

## Security notes

- Always run behind HTTPS (Caddy profile included, or your own nginx).
- One lane per agent/team; never share keys across lanes — a message sent
  through someone else's lane appears **as them** in the chat.
- Rotate a lane's key with **New key** from the UI the moment a person leaves the trust
  circle; hand keys over via a secret manager or DM, never in the group chat
  or a repo.
- The SQLite file contains bot tokens and API keys — protect `data/` like a
  secrets store (backups included).
- Webhook endpoints are authenticated with per-lane secret tokens
  (`X-Telegram-Bot-Api-Secret-Token`), so spoofed POSTs are rejected.

## License

[MIT](LICENSE)
