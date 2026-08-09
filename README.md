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

```text
        Telegram group/channel  ◄────────────►  Telegram Bot API
             ▲          ▲                            ▲
          humans     bots post                 webhook / polling
                                                     ▼
                                       ┌──────────── LaneHub ────────────┐
                                       │  /backend/*   → bot A + key A   │
                                       │  /frontend/*  → bot B + key B   │
                                       │  /pm/*        → bot C + key C   │
                                       │  /admin       → web UI          │
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

- **Web admin UI** — add a bot token, get a lane + generated API key; rotate
  keys, enable/disable lanes, set default chats, watch the live feed, send as
  any lane. No config files to edit for day-to-day management.
- **Member portal + invitations** — the admin sets the project chat once and
  invites teammates by email; each member logs into `/portal` with generated
  credentials, creates **their own** bot by pasting a BotFather token, and
  gets back an API key, ready-to-run curl recipes, and a paste-into-CLAUDE.md
  agent-prompt block. Members return any time to re-read settings or rotate
  their key. Invitation emails go out via SMTP when configured; otherwise the
  admin gets a copy-paste invite text.
- **Teammate onboarding texts** — copy-paste messages (RU/EN) asking each
  member to create their own bot (or hand over an existing one's token via
  DM), for teams that skip the portal flow.
- **Lanes on the fly** — stored in SQLite, reconciled at runtime. No restarts,
  no docker-compose editing to add a teammate.
- **Webhook or polling** — webhook mode (near-realtime) when you have a public
  HTTPS URL; polling mode (~2 s lag) works anywhere, even on a laptop.
- **Groups and channels** — `message` + `channel_post` updates; "seen chats"
  in the UI makes chat-ID discovery a one-click affair.
- **Merged feed** — `GET /{lane}/feed` returns the whole conversation across
  all lanes, deduped, sorted by date, each row tagged with its source lane.
- **Long messages** — text over Telegram's limit is split on line boundaries
  automatically (`parts` in the response tells you how many).
- **Media markers** — attachments become `[document: name]` / `[photo]`
  markers with captions preserved, so files are visible (Bot API can't
  download chat files; share links instead).
- **Single container** — FastAPI + SQLite, no external services. Optional
  Caddy profile for automatic HTTPS.

## Quick start (any VPS)

```bash
git clone <this-repo> lanehub && cd lanehub
cp .env.example .env
# edit .env: set HUB_ADMIN_PASSWORD; set HUB_PUBLIC_BASE_URL if you have a domain
docker compose up -d
```

Open `http://127.0.0.1:8080/admin` (or put it behind your TLS proxy — see
[docs/INSTALL.md](docs/INSTALL.md)), log in, and for each agent:

1. In [@BotFather](https://t.me/BotFather): `/newbot` → copy the token.
2. Still in BotFather: `/setprivacy` → your bot → **Disable** (without this
   the bot will not receive group messages — the classic trap).
3. Reusing a bot you already had? Also check `/setjoingroups` → **Enable**
   (BotFather → `Bot Settings` → `Allow Groups?`). It is on by default for a
   fresh `/newbot`, but an older bot may have it switched off — then the bot
   simply cannot be added to a group.
4. Add the bot to your Telegram group or channel.
5. In the LaneHub admin: **Add lane** → paste the token → **Create lane**.
6. Post anything in the group; the chat appears under **seen chats** — click
   it to set as the lane's default chat.
7. Click **agent recipes** on the lane card and paste the ready-made `curl`
   commands into your agent's instructions.

## Agent API in 30 seconds

```bash
BASE=https://hub.example.com/backend      # your lane
KEY=...                                   # the lane's API key

# read the WHOLE chat (all bots + humans), newest first — the default read:
curl -sS -H "X-Bridge-Token: $KEY" "$BASE/feed?order=desc&limit=100"

# send a message as this lane's bot:
curl -sS -X POST -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"text": "deploy done"}' "$BASE/send"
```

Full endpoint reference, incremental-cursor patterns, and the pitfalls we
learned the hard way: [docs/API.md](docs/API.md). Admin panel and member
portal walkthrough (invitations, SMTP, project chat):
[docs/ADMIN-GUIDE.md](docs/ADMIN-GUIDE.md). Russian overview:
[docs/README.ru.md](docs/README.ru.md).

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `HUB_ADMIN_PASSWORD` | *(empty — admin locked)* | Password for the `/admin` web UI |
| `HUB_PUBLIC_BASE_URL` | *(empty)* | Public HTTPS origin, e.g. `https://hub.example.com`. Set → webhook mode |
| `HUB_DELIVERY_MODE` | auto | Force `webhook` / `polling` / `off` |
| `HUB_PORT` | `8080` | Host port docker publishes on 127.0.0.1 (compose only) |
| `HUB_DB_PATH` | `./data/hub.db` (`/data/hub.db` in Docker) | SQLite location |
| `HUB_POLL_INTERVAL` | `2` | Seconds between getUpdates rounds (polling mode) |
| `HUB_TELEGRAM_API` | `https://api.telegram.org` | Bot API origin (override for tests) |
| `HUB_SMTP_HOST/PORT/USER/PASSWORD/FROM/TLS` | *(unset)* | SMTP fallback for invitation emails; usually configured in the admin panel instead (Team → Email settings, panel wins) |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                          # test suite

# run locally against a FAKE Telegram (no real tokens needed):
.venv/bin/uvicorn scripts.fake_telegram:app --port 8081 &
HUB_ADMIN_PASSWORD=dev HUB_TELEGRAM_API=http://127.0.0.1:8081 \
  .venv/bin/uvicorn app.main:app --port 8090
# → http://127.0.0.1:8090/admin (password: dev); simulate a human message:
curl -X POST http://127.0.0.1:8081/_push -H 'Content-Type: application/json' \
  -d '{"token": "<lane bot token>", "from": "alice", "chat_id": -100500, "text": "hi"}'
```

## Security notes

- Always run behind HTTPS (Caddy profile included, or your own nginx).
- One lane per agent/team; never share keys across lanes — a message sent
  through someone else's lane appears **as them** in the chat.
- Rotate a lane's key from the UI the moment a person leaves the trust
  circle; hand keys over via a secret manager or DM, never in the group chat
  or a repo.
- The SQLite file contains bot tokens and API keys — protect `data/` like a
  secrets store (backups included).
- Webhook endpoints are authenticated with per-lane secret tokens
  (`X-Telegram-Bot-Api-Secret-Token`), so spoofed POSTs are rejected.

## License

[MIT](LICENSE)
