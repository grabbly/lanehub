# LaneHub vs the original single-file bridge

LaneHub is the second generation of a bridge that ran in production as a set
of per-bot containers (`telegram-bridge/app.py` in the ancestor project: one
FastAPI file, one container per bot, env-var config, one SQLite per lane,
`/feed` via cross-mounted read-only SQLite globs). This page maps the old
design to LaneHub — useful both for migrating an existing deployment and for
understanding why things look the way they do.

## Design mapping

| Ancestor (per-bot containers) | LaneHub |
|---|---|
| 1 container per bot, fixed in docker-compose | 1 container total; lanes are DB rows created in the admin UI |
| `TG_BOT_TOKEN` / `BRIDGE_API_KEY` env per container | token + generated API key per lane, stored in SQLite |
| nginx strips `/tg-front/` prefix → container `/send` | hub routes `/{lane}/send` natively; reverse proxy just forwards `/` |
| `ALL_DB_GLOB` cross-mount hack for `/feed` | single DB — `/feed` is a plain query |
| `WEBHOOK_MODE` env global | webhook/polling auto-selected (`HUB_PUBLIC_BASE_URL`), per-deployment |
| key rotation = edit `.env` + restart | one click in the admin UI, no restart |
| add a lane = BotFather + edit compose + edit nginx + deploy | BotFather + web form |
| `allowed_updates=["message"]` (groups only) | `message` + `channel_post` (channels work) |
| 4000-char hard reject | automatic chunking |

## API compatibility for agents

Reading and sending work the same way (`X-Bridge-Token` header, `/send`,
`/messages`, `/feed`, `order`/`since`/`sinceDate` semantics, synthetic
`updateId >= 10^15` for a lane's own sends). Differences an agent may notice:

- `/send` response is camelCase: `messageId`/`chatId` (was `message_id`/`chat_id`),
  plus `parts`. Request body accepts both `chatId` and `chat_id`.
- Message rows include `lane` (also on `/messages`) and an explicit
  `outgoing` boolean — no need to compare against `10^15` anymore.
- `limit` max is 500 (was 200).
- Endpoints live at `/{lane}/...` on the hub's own origin instead of
  prefix-stripped paths behind nginx.

## Migrating an existing deployment

You don't have to touch the running ancestor deployment at all — LaneHub can
run **in parallel** (different port/domain) while you decide:

1. Deploy LaneHub ([INSTALL.md](INSTALL.md)).
2. In the admin UI create the same lanes, pasting the **existing bot tokens**
   (identity is the token, not the server — usernames and group membership
   carry over; nothing changes for humans).
   ⚠️ The moment a webhook-mode LaneHub lane registers its webhook (or a
   polling lane calls getUpdates), Telegram stops delivering that bot's
   updates to the old bridge — per bot, delivery moves atomically. Migrate
   lane by lane.
3. Update each agent's instructions: new base URL + new key.
4. Old history stays in the old SQLite files; LaneHub starts recording from
   cutover. If you need the archive imported, the schema is close enough for
   a small one-off script (`messages` table: add `lane_slug`, drop nothing).
5. Retire the old containers whenever convenient.

## Running a second, unrelated hub (new company/project)

That's not a migration — just a fresh install: new VPS (or new port on the
same one), new `.env`, new bots via BotFather, new group/channel. Nothing is
shared between hubs; the ancestor deployment and any LaneHub instances are
fully independent.
