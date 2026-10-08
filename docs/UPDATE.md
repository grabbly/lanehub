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

## Version-specific notes

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
- **Media attachments:** Telegram `media.fileId` attachments remain downloadable through the hub's file proxy only if the same bot token is still used.

