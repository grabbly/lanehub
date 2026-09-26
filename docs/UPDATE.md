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
