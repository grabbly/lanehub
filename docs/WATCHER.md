# Waking a Claude Code session on `@mention`

By default a LaneHub agent is *pull-based*: your `claude` session reads `/feed`
and posts to `/send` when it happens to run. There is no live process listening
to the chat. This guide adds that missing piece — so when a human writes
**`@<your_bot_username>`** in the group, the **same** Claude Code session
resumes and answers, with all its accumulated context.

## How it works

```
human: "@denis_team_bot глянь деплой"
   → Telegram → LaneHub ingest
   → LaneHub detects @bot_username, queues a wake, remembers the lane's session id
watcher (this repo, runs next to your project):
   1. GET  /{lane}/wake        → { text, sessionId }
   2. claude --resume <sessionId> -p "<text>"     (empty id ⇒ fresh session)
   3. reads the new session id from claude's JSON output
   4. POST /{lane}/wake/ack    { wakeId, sessionId }
   → the resumed session replies through /send, as its CLAUDE.md already says
```

**All state lives in LaneHub** (the wake cursor and the current session id, per
lane). The watcher holds nothing, never touches Telegram, and is disposable —
restart it, move it, or point one process at many lanes. The session that
answers is always *the one the bot last replied from*, because each answer
resumes the previous one. See [API.md](API.md) for the `/wake` endpoints.

## Prerequisites

- The bot's **privacy mode is OFF** in BotFather (`/setprivacy` → Disable),
  otherwise the bot never sees `@mentions` in a group — the classic trap.
- `claude` (Claude Code CLI) is installed **on the machine that runs the
  watcher**, and that machine holds the project's working directory. Claude Code
  sessions are stored per project directory on the local machine, so the watcher
  must run where the session should live.

## Setup — one project (the easy path)

The watcher is a single stdlib-only file. Grab it, set three env vars, run it —
no config file, no `pip install`:

```bash
# 1. get the one file straight from your hub (always matches the deployed version)
curl -sSO https://hub.example.com/watcher.py
#    (or: raw.githubusercontent.com/grabbly/lanehub/main/scripts/telegram_watch.py,
#     or your checkout's scripts/telegram_watch.py)

# 2. point it at your lane
export LANEHUB_BASE=https://hub.example.com/backend   # your lane URL (slug included)
export LANEHUB_KEY=...                                 # your lane API key — never commit
export CLAUDE_PROJECT_DIR=/path/to/your/project        # where your CLAUDE.md + claude live

# 3. run
python3 watcher.py
```

The exact copy-paste for each lane (with your hub and lane URLs filled in) is in
the **agent recipes** dialog in the admin panel and in the member portal — it is
part of the CLAUDE.md block, so your agent can start and stop the watcher itself.

That's the whole install. Now write `@your_bot_username` in the group and the
watcher resumes your Claude Code session to answer. To keep it running after you
log out, use the systemd unit below.

## Setup — several projects on one machine

Use a JSON config instead of env vars:

1. Copy the example config and fill in your lane(s):

   ```bash
   cp scripts/watch.config.example.json watch.config.json
   ```

   ```json
   {
     "poll_interval": 5,
     "claude_bin": "claude",
     "lanes": [
       {
         "base": "https://hub.example.com/backend",
         "key_env": "LANEHUB_KEY_BACKEND",
         "project_dir": "/srv/projects/backend",
         "extra_args": ["--permission-mode", "acceptEdits"]
       }
     ]
   }
   ```

   - `base` — your lane URL, slug included (same one your agent already uses).
   - `key_env` — name of the env var holding the lane API key. Prefer this over
     an inline `"key"` so real keys never land in the file. `watch.config.json`
     is gitignored regardless.
   - `project_dir` — the working directory to run `claude` in (where the
     session and its CLAUDE.md live).
   - `extra_args` — optional; passed through to `claude` (e.g. permission mode).

2. Export the key(s) and run:

   ```bash
   export LANEHUB_KEY_BACKEND=...        # never commit this
   python3 scripts/telegram_watch.py --config watch.config.json
   ```

The first mention with an empty stored session id starts a fresh session; its
id is reported back and every later mention continues it. If a stored session id
ever goes missing, the watcher starts a fresh one and reports that — no manual
recovery.

## One watcher or several?

Sessions are local to the machine that runs `claude`, so the rule is
**one watcher per machine**, listing every lane whose `project_dir` is on that
machine:

- All your projects on one host → **one** watcher, many entries in `lanes`.
- Projects spread across hosts → one watcher per host, each listing only its
  local lanes.

Because the session mapping lives in LaneHub (not the watcher), you can restart
or relocate a watcher freely. Run only **one** watcher per lane, and don't open
that lane's session interactively by hand while the watcher runs — two
concurrent `--resume` of the same id corrupt the session.

## Run it as a service (systemd)

One project (env mode):

```ini
# /etc/systemd/system/lanehub-watch.service
[Unit]
Description=LaneHub @mention → Claude Code watcher
After=network-online.target

[Service]
Environment=LANEHUB_BASE=https://hub.example.com/backend
Environment=LANEHUB_KEY=...
Environment=CLAUDE_PROJECT_DIR=/srv/projects/backend
ExecStart=/usr/bin/python3 /srv/lanehub/telegram_watch.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Several projects (config mode): drop the three `Environment=` lines and use
`ExecStart=/usr/bin/python3 scripts/telegram_watch.py --config /srv/lanehub/watch.config.json`
with a `WorkingDirectory=/srv/lanehub`.

```bash
sudo systemctl enable --now lanehub-watch
journalctl -u lanehub-watch -f          # follow the logs
```

Run as a user service instead (`systemctl --user`, unit in
`~/.config/systemd/user/`) if `claude` is authenticated under your user account
rather than root.

## Control & transparency

**See what it's doing.** Every wake is logged: the mention, which session is
resumed, success/failure, and each ack. Under systemd: `journalctl -u
lanehub-watch -f`. Run in the foreground and it logs straight to your terminal.

**Preview without side effects.** `--dry-run` polls and logs what it *would* do
but never runs `claude` and never acks — nothing is consumed, so it's safe to
leave running while you watch:

```bash
python3 telegram_watch.py --dry-run
```

**One pass, then exit** (handy for cron or a quick check): `--once`.

**Server-side state — no need to shell into the watcher's machine.** LaneHub
holds the truth, so you can inspect it from anywhere with the lane key:

```bash
python3 telegram_watch.py --status          # pretty per-lane summary
# or straight from the API:
curl -sS -H "X-Bridge-Token: $KEY" "$BASE/info" | jq .wake
# → { "armed": true, "cursor": 812, "claudeSessionId": "…", "pendingMention": null }
```

`armed` = a watcher has polled at least once; `pendingMention` = a mention is
waiting to be handled right now (non-null usually means the watcher is busy,
stopped, or not running).

**Stop it.**

- Foreground: `Ctrl+C` (it shuts down cleanly).
- systemd: `sudo systemctl stop lanehub-watch` (and `disable` to keep it off
  across reboots).
- Backgrounded manually: `kill <pid>` (find it with `pgrep -f telegram_watch`).

**Pause one lane without touching the watcher.** Disable the lane in the admin
UI (or `PATCH /admin/api/lanes/<slug> {"enabled": false}`). LaneHub then rejects
`/wake` for it and the watcher just logs and skips — re-enable to resume. The
session id and cursor are preserved, so it picks up exactly where it left off.

**Reset the session lineage.** A genuinely lost/deleted session self-heals to a
fresh one on the next mention — no action needed. To force a fresh start on
purpose, clear the lane's stored `claude_session_id` state (admin/DB); the next
mention then begins a new session and records its id.

## Migrating existing agents

The chat contract is unchanged — agents still read `/feed` and post to `/send`,
so **nothing in anyone's CLAUDE.md needs to change** and no existing lane
breaks. To turn on @mention-waking for a teammate:

1. Deploy the LaneHub version that has the `/wake` endpoints (this one).
2. On the machine where their `claude` runs, add their lane to a
   `watch.config.json` and start the watcher (or add them to an existing host
   watcher's `lanes` list and restart it).

That's it — no key rotation, no re-onboarding.

### Copy-paste for a teammate

Send this to a teammate who already has their lane working (they have their
`LANEHUB_BASE` and `LANEHUB_KEY`). It installs and runs the watcher on their
machine, next to their project:

```
Хочешь, чтобы бот отвечал, когда его зовут через @имя_бота в чате? Запусти у себя вотчер:

# 1. скачай один файл с хаба (зависимостей нет, нужен только python3)
curl -sSO https://hub.example.com/watcher.py

# 2. укажи свой лейн и папку проекта
export LANEHUB_BASE=<твой лейн, напр. https://hub.example.com/backend>
export LANEHUB_KEY=<твой ключ лейна>            # в код/репо не клади
export CLAUDE_PROJECT_DIR=<путь к папке проекта, где твой CLAUDE.md>

# 3. запусти (в фоне)
nohup python3 watcher.py > watcher.log 2>&1 &

Статус:     python3 watcher.py --status
Остановить: kill $(pgrep -f watcher.py)

Всё — пиши @имя_бота в группе, и твоя сессия Claude Code продолжится и ответит.
```
