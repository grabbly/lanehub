# Updating an existing LaneHub deployment

LaneHub is a single container (FastAPI + SQLite). Updating is pull + rebuild;
the database (lanes, keys, history) lives in a mounted volume and is kept.
Schema changes are applied automatically on start — copy `data/hub.db` before
updating anyway. Read the version notes below and
[CHANGELOG.md](../CHANGELOG.md) first.

## Update

On the host that runs the hub:

```bash
cd /path/to/lanehub        # e.g. /home/gabby/lanehub
git pull
docker compose up -d --build
```

Verify it came back up:

```bash
curl -sS http://127.0.0.1:8080/api/session
# → {"authenticated":false,"role":null,...,"adminPasswordSet":true}
curl -sS http://127.0.0.1:8080/health   # → {"status":"ok"}
```

(Use your public URL instead of `127.0.0.1:8080` if you curl from outside.)

## Rollback

Redeploy the previous commit — the database is not migrated destructively, so
downgrading is safe:

```bash
git log --oneline -5      # find the previous commit
git checkout <prev-sha>
docker compose up -d --build
```

## Without Docker

If you run the hub straight from a virtualenv, reinstall dependencies on
every update — new versions may add packages, and the app won't start
without them (0.5 added `python-multipart`):

```bash
git pull
.venv/bin/pip install -r requirements.txt
# then restart your uvicorn / systemd service
```

## Moving the hub to another server

A Telegram bot has one webhook, and whoever holds the bot's token can set it.
A copy of the hub left on the old server — stopped, but with a restart policy
— comes back after the next reboot of that machine, sets the webhook to
itself and silently takes every update away from the new hub. So:

1. **Back up** `data/hub.db` on the old server (`sqlite3 data/hub.db ".backup hub.db.bak"`).
2. **Stop and disable the old hub** — `docker compose down` in its directory,
   or for a single container `docker update --restart=no <container>` and
   `docker stop <container>`. A plain `docker stop` is not enough: the
   container keeps its restart policy.
3. Copy `data/hub.db` and `.env` to the new server, set `HUB_PUBLIC_BASE_URL`
   to the new address and, if the old address was different, list it in
   `HUB_PREVIOUS_BASE_URLS` so the new hub moves the webhooks to itself.
   Start it.
4. **Revoke every bot token** — @BotFather → the bot → API Token → Revoke —
   and paste the new one into the lane card (**More settings** → **Bot token
   → Replace token**). Only this makes the old copy harmless for good, and it
   also retires tokens that pre-0.5 versions wrote into `docker logs`.
5. Messages the old copy received while it held a bot can be copied over —
   see [Moving messages from another hub](#moving-messages-from-another-hub).
6. Check each lane card: no red **updates go elsewhere** badge. The watchdog
   (0.7.1+) raises it within minutes if anything takes a bot later.

## Version-specific notes

### 0.14.2 — «＋ Add bot» picker visible again

- **Nothing to do:** panel CSS fix only, no schema, setting or API changes.

### 0.14.1 — silent messages

- **Nothing to do:** additive API field, no schema or setting changes.
- `POST /{lane}/send` takes `disableNotification: true` to deliver a message silently (no sound/vibration). Without the field messages are sent exactly as before.

### 0.14.0 — deposit history on the client page

- **Nothing to do:** no schema, setting or API-breaking changes; existing boards get it on update.
- On the client page, the «Розподілено» and «Залишок» tiles open «Куди пішов депозит»: deposit top-ups/refunds and the cards paid from the deposit (archived included), newest first, with a footer that equals the tiles.
- `data.json` gains `depositHistory` (whitelisted fields only). Cards hidden from the client are summed into one «Інші роботи» line, never named. Ledger notes are not shown to the client.

### 0.13.0 — client comment with the ✓ Done tick

- **Nothing to do:** new column `board_tasks.client_done_comment` is added automatically on startup.
- Clients can optionally attach a short note (up to 500 characters) when marking a task as done, and add or edit it afterwards.
- Added explanatory hint line under the done button on the client page (`client_done_hint`).
- Endpoint `POST /{board_slug}/b/{token}/tasks/{task_id}/done` accepts optional `"comment"` and returns `{id, clientDoneAt, clientDoneComment}`.
- Operator panel displays the comment on cards and in the task dialog.
- Agents and admin see `clientDoneComment` on tasks; comment is cleared together with `clientDoneAt`.

### 0.12.1 — waiting cards only in the banner

- **Nothing to do:** client page change only (no schema, API or setting changes).
- On the client page, `waiting` cards are listed once — in the «Потрібно від вас» banner; the separate waiting column is no longer shown there. The operator panel is unchanged.

### 0.12.0 — client ✓ Done tick

- **Nothing to do:** new column `board_tasks.client_done_at` is added automatically on startup.
- Existing boards and API clients keep working unchanged.
- Cards in `waiting` column get a "✓ Зроблено / Done" tick on the client page without moving the card or sending notifications.
- Added client write endpoint `POST /{board_slug}/b/{token}/tasks/{task_id}/done` with in-memory rate limiting.
- Automatic reset of `client_done_at` when a card leaves `waiting`.
- Operator panel highlights client-done cards with a chip, header counter/filter, and clear mark action in the card dialog.
- Agent API exposes `clientDoneAt` and allows clearing via `{"clientDone": false}`. See [BOARDS.md](BOARDS.md).

### 0.11.0 — client panel

- **Nothing to do:** new columns (`board_tasks.archived_at`, `boards.status_i18n`,
  `boards.status_updated_at`) and indexes are added automatically on startup.
- Existing boards and API clients keep working unchanged.
- Adds manual archiving for tasks, status line «Де ми зараз» (`PUT /{lane}/board/status`),
  client panel highlights (status card, waiting banner, new badges, monthly archive section),
  and operator panel archive / status tools. See [BOARDS.md](BOARDS.md).

### 0.10.0 — voice messages as text (optional)

- **Nothing to do** if you don't set `HUB_STT_URL`: the feature stays off and
  the feed looks as before. The update adds one table (`transcripts`).
- **To turn it on:** set `HUB_STT_URL` (and `HUB_STT_KEY`, and for OpenAI
  `HUB_STT_MODEL=whisper-1`) in `.env`, recreate the container. New voice,
  audio and video-note messages are transcribed in the background; agents see
  `[voice 0:39] text…` in `text`, plus `transcript` / `transcriptStatus`.
- Older voice messages: `POST /admin/api/transcripts/backfill {"hours": 72}`
  while signed in, or `POST /{lane}/transcribe/{fileId}` from an agent.

### 0.9.0 — Boards (optional)

- **Nothing to do** if you don't want boards: the module is off by default,
  no routes, no panel tab. The update only adds new tables (`boards`,
  `board_tasks`, `board_ledger`, `board_events`); existing data is untouched.
- **To use boards:** set `HUB_BOARDS=1` in `.env` and recreate the container
  (`docker compose up -d`). A **Boards** tab appears in the panel. See
  [BOARDS.md](BOARDS.md).
- **Client links** are `/{board}/b/{token}/` on the hub's own domain. To serve
  them on another domain (or keep an older link), proxy that domain to the hub —
  example in [BOARDS.md](BOARDS.md#the-client-link).
- Importing an existing board: `scripts/import_board.py` (back up
  `data/hub.db` first).

### 0.8.0 — Autopilot

- **Mentions are answered only while a lane's autopilot is ON** — after the
  update it is off everywhere, so a watcher that used to answer around the
  clock goes quiet. Switch it on: **Start** in the macOS menu-bar app
  ([macos/README.md](../macos/README.md)), `./tg-autopilot.sh on [hours]`, or
  `POST /{lane}/autopilot {"on": true}` (8 h by default, 72 h max).
- **systemd watchers:** use `Restart=on-failure` instead of `Restart=always`;
  the new watcher exits when autopilot is off.
- **Set the owner** (Telegram username) on each bot card so chat requests
  (`/autopilot @bot`) reach them.
- Re-download `telegram_watch.py` and the helpers from the hub
  (`curl {hub}/watcher.py`, `curl {hub}/tg-autopilot.sh`).

### 0.7.1 — webhook watchdog

- **Nothing to do** for a hub that stays where it is.
- **If you moved the hub to a new domain** and Telegram may still send to the
  old one: list the old origin in `HUB_PREVIOUS_BASE_URLS` before updating.
  Earlier versions recognised any address ending in `/{slug}/webhook` as
  their own; 0.7.1 only trusts its exact address and that list, and treats
  any other one as another system (the lane goes send-only — press
  **Take back** on its card to fix it).
- New optional settings: `HUB_PREVIOUS_BASE_URLS`, `HUB_WEBHOOK_CHECK_INTERVAL`
  (300 s), `HUB_WAKE_MAX_AGE_HOURS` (24 h) — see [.env.example](../.env.example).
- Mentions older than 24 h no longer wake the agent; set
  `HUB_WAKE_MAX_AGE_HOURS=0` to keep the old behaviour.

### 0.6.0 — one bot, many chats

- **Nothing to do.** Lanes that share a bot token and were made send-only by
  0.5.4/0.5.5 (because another lane of the same bot held the webhook) go back
  to `hub` on the first start, and the webhook moves to the bot's oldest
  enabled lane. Their wake cursor is reseeded, so mentions posted while the
  hub restarts may be skipped once.
- A bot whose webhook points at another system is left alone, as before —
  all of its lanes stay send-only.
- Messages captured before the update stay under the lane that received
  them; `/feed` shows them in the right chat anyway.

### 0.5.4 — send-only lanes

- **Nothing to do for bots made for the team chat.** Their webhook points at
  this hub, so they stay `hub` lanes.
- **Bots that another system receives** (a product bot, an app's bot) are
  switched to **send-only** automatically on the first start of 0.5.4 —
  *provided that system holds the bot's webhook at that moment.* The hub
  never touches their webhook again.
- **If an older hub already took such a bot over** (its `getWebhookInfo`
  shows this hub's `/{lane}/webhook`), 0.5.4 can't tell it apart from a normal
  lane. Either: (a) before updating, point the webhook back to the other
  system (it re-registers its own URL); or (b) after updating, click **make
  send-only** on the lane card first, then restore the other system's
  webhook. Check with the card's **webhook status**: `url` must be the other
  system's.
- **One bot in several lanes** (same token, one lane per chat) — *since
  0.6.0 all of them receive, see above.* In 0.5.4/0.5.5 only one lane
  can hold the bot's webhook, so on start the others are switched to
  send-only. That's expected — each still posts to its own chat, reads it via
  `/feed` and gets its mentions (0.5.5+ only from its own chat). If you
  delete the lane that holds the webhook, switch one of the others back to
  **hub** on its card.
- The system that owns such a bot can learn about `@mentions` by polling
  `GET /{lane}/wake` and acking (see [API.md](API.md)).

### 0.5 — feed isolation, seq cursor, sendFile

- **Update to 0.5.2 or later, not 0.5.0.** 0.5.0 (`de3ff57`) left the feed
  showing a single row on a hub with existing history; 0.5.1+ repairs such a
  database on start. The migration (new `messages` columns, `seq` numbered in
  date order) runs once, automatically; take a copy of `data/hub.db` first
  anyway.
- **Each lane's `/feed` now shows only its bound chat plus its own bot's
  DMs.** Messages from other chats the bot sits in, and other lanes' DMs, are
  no longer in it. An unbound lane sees only its DMs — bind every lane.
- **Binding checks the chat with Telegram.** The bot must already be in the
  chat when you bind it (otherwise the panel shows an error). Ids missing the
  `-100` prefix are fixed, `@channel` is stored as its numeric id.
- **Lanes bound by `@channelname` are converted to the numeric id** on the
  first start (or first `/feed` call). Agents that still send
  `"chatId": "@channelname"` keep working.
- **Agents:** old `tg-fetch.sh` / `tg-report.sh` keep working. Re-download
  the helpers from the hub to get the seq cursor and `tg-send-file.sh`.
- **Revoke bot tokens.** Before 0.5 every lane's bot token was written to
  `docker logs`. After updating, revoke each token in @BotFather (API Token →
  Revoke), paste the new one into the lane card (**Bot token → replace**), and
  run `docker compose up -d --force-recreate` to drop the old logs.
- **Also part of 0.5:** the single-operator login and the @mention watcher
  (next section).

### 0.5 (also) — single-operator login + @mention watcher

- **Single-operator login.** There is **one sign-in form at `/`**: enter
  `HUB_ADMIN_PASSWORD` (password only). The member portal, Team tab, email
  invitations and SMTP were removed — previously invited members can no longer
  log in, and the operator manages every lane from the panel. `/admin` still
  redirects to `/`; `/portal` is gone (404).
- **`HUB_ADMIN_PASSWORD` must be set** in `.env` (unchanged requirement — an
  empty value locks sign-in).
- **No database migration.** New per-lane state (`wake_cursor`,
  `claude_session_id`) is written lazily into the existing `lane_state` table.
  Existing lanes, API keys and message history are untouched; the now-unused
  `members` table is left in place as harmless dead data.
- **@mention waking is opt-in and runs on agent machines, not the hub.** After
  updating, the hub serves the watcher at `GET /watcher.py` and each lane's
  CLAUDE.md agent-prompt block includes start/stop commands. Nothing on the hub
  needs to run for existing behaviour (reading `/feed`, posting to `/send`) to
  keep working. To enable it, see [WATCHER.md](WATCHER.md).

## Moving messages from another hub

When migrating to a new LaneHub deployment or moving between servers, you can copy message history from an old hub's SQLite database (`hub.db`) into this hub:

1. Copy the old database file (e.g. `old-hub.db`) into the new hub's `./data` directory on the host.
2. Run the import script inside the container:

```bash
docker compose exec lanehub python scripts/import_messages.py /data/old-hub.db --since <unix>
```

Options:
- `--since <unix>`: import only messages newer than this timestamp (default: `0`, all history).
- `--lane OLD_SLUG[=NEW_SLUG]`: import specific lanes, optionally renaming the slug. Repeatable. If omitted, all lanes whose slugs exist in the target database are imported; rows of non-existent lanes are skipped.
- `--dry-run`: simulate the import and report message counts without modifying the database.

Notes:
- **Deduplication:** messages with the same `(lane_slug, update_id)` already present in the target database are automatically skipped.
- **Feed cursor:** imported messages are stored with `store_message()`, which assigns a new monotonic `seq` cursor so imported rows show up to downstream agents following the feed cursor.
- **Media attachments:** a `media.fileId` belongs to the bot that received the message, so imported attachments stay downloadable through `/file` as long as the lane uses the same bot (a revoked and re-issued token is fine).

