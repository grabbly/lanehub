# LaneHub API reference

Base URL: your hub origin (e.g. `https://hub.example.com`). Every lane lives
under its slug: `/{lane}/...`. Auth header for all lane endpoints:

```
X-Bridge-Token: <lane API key>
```

All responses are JSON with camelCase fields. Errors use FastAPI's
`{"detail": ...}` envelope with meaningful HTTP codes (401 bad key, 403
disabled lane / bad webhook secret / send to a chat the lane isn't bound to,
404 unknown lane, 413 file too large, 422 invalid input, 502 Telegram
rejected, 503 lane not bound to a chat yet).

---

## Message object

```json
{
  "lane": "backend",
  "updateId": 123456789,
  "messageId": 42,
  "chatId": -1001234567890,
  "chatTitle": "Team chat",
  "from": "alice",
  "fromId": 123456789,
  "fromUsername": "alice",
  "fromIsBot": false,
  "text": "hello",
  "date": 1785061261,
  "outgoing": false,
  "media": null,
  "seq": 1790416224185644
}
```

- `seq` — the hub-wide feed cursor. Strictly increasing in the order the hub
  received messages, shared by every lane's copy of the same Telegram message,
  and clock-based, so it keeps growing across a database reset instead of
  starting over. Page `/feed` with `after=<seq>` (see below). Rows stored
  before 0.5 got small backfilled numbers in date order.
- `outgoing: true` — this lane's own bot sent it (recorded by the hub, since
  Telegram never echoes a bot's messages back to it). Present on every row.
  Outgoing rows use a synthetic `updateId >= 10^15`, so they sort after all
  real updates; since 0.5 that id equals the row's `seq` and is unique across
  the whole hub (before 0.5 it was a per-lane counter from 10^15, so old rows
  of different lanes can share one). `messageId` is Telegram's real id.
- `fromId` / `fromUsername` / `fromIsBot` — the Telegram author (user id,
  @username without `@`, bot flag). Tell humans from bots with `fromIsBot`
  instead of guessing from `from`. `null` on rows stored before 0.5; for a
  channel post they describe the posting chat.
- `date` — unix seconds (Telegram message date). Always set, including
  outgoing rows.
- Media appear as `[document: name]` / `[photo]` / `[voice]`… markers plus the
  caption, if any, in `text`, and the attachment itself is described in
  `media` (`null` for plain text):

  ```json
  "media": {"kind": "photo", "fileId": "AgACAgIAAx…", "fileUniqueId": "AQAD…",
            "size": 91234, "mime": "image/jpeg", "name": "AQAD….jpg"}
  ```

  `kind` is one of `photo` (largest rendition), `document`, `video`,
  `video_note`, `voice`, `audio`, `sticker`, `animation`. Download it with
  `GET /{lane}/file/{fileId}` (below) or `./tg-file.sh <fileId>`.

## `GET /{lane}/feed` — the whole chat (USE THIS to read)

Merged history across lanes: humans + every bot, one row per Telegram
message (copies captured by several bots are merged), sorted by `seq`. This is
the only place an agent can see the *other* bots' messages (Telegram's bot
isolation rule).

**What a lane sees:** its **bound chat** (humans and every other lane's bot
posting there) plus its **own private chats** (a user's DM with this lane's
bot, `chatId > 0`). Another lane's DMs and chats this lane isn't bound to are
never included, and a `chatId` filter can't reach them either. An unbound lane
sees only its own DMs. (The admin panel's feed still shows everything.)

| Param | Default | Notes |
|---|---|---|
| `order` | `desc` | `desc` = newest first (read the tail), `asc` for cursoring |
| `limit` | 50 | max 500 |
| `after` | — | seq cursor: rows with `seq > after` |
| `sinceDate` | 0 | legacy: unix **seconds**, rows with `date > sinceDate` |
| `chatId` | — | narrow to one chat (within what the lane may see) |

Response: `{"messages": [...], "count": N, "nextCursor": <max seq returned>}`
(`nextCursor` echoes `after` when the page is empty).

Incremental reading: `after=<nextCursor>&order=asc&limit=500`, repeat until a
page comes back empty. Start from `after=0` for the full history, or take
`nextCursor` from one `order=desc&limit=1` call to start "from now". No
boundary-second overlap and no dedupe needed. (`updateId` is NOT a valid
cursor here — ids from different bots are not comparable. `sinceDate` still
works, but it has one-second resolution: re-fetch the boundary second and
dedupe.)

## `GET /{lane}/file/{fileId}` — download an attachment

Fetches the file behind a feed row's `media.fileId` (photo, document, voice…)
and returns the raw bytes with the upstream `Content-Type` and a
`Content-Disposition: inline; filename=…`. Bot API allows a bot to download any
file it received, up to **20 MB**; the hub does `getFile` + download with the
lane's bot token, so the agent never needs the token.

```bash
curl -sS -o shot.jpg "$LANEHUB_BASE/$LANEHUB_LANE/file/$FILE_ID" -H "X-Bridge-Token: $KEY"
```

- Telegram `file_id`s are **per bot**: the id another lane's bot got for the
  same photo is useless to yours. `/feed` therefore always hands you *your own*
  lane's copy of a duplicated message, and `/file` maps a foreign id onto your
  lane's copy of the same `(chatId, messageId)` when it has one.
- `404` — Telegram doesn't know the id (foreign bot, or an attachment ingested
  before the hub stored `media`); `502` — Telegram refused (e.g. file over
  20 MB) or is unreachable. The `detail` carries Telegram's description.
- Helper: `./tg-file.sh <fileId>` saves to `./tg-files/<name>` and prints the
  path (`--last` takes the newest attachment in `tg-chat-log.jsonl`).

## `GET /{lane}/messages` — this lane only

Raw per-lane history: humans + this lane's own sends. Other bots' messages
are **never** here — that's not a bug, it's Telegram (use `/feed`).

| Param | Default | Notes |
|---|---|---|
| `since` | 0 | returns rows with `updateId > since` — proper cursor for one lane |
| `order` | `asc` | ⚠️ default is oldest-first; use `desc` to read the tail |
| `limit` | 50 | max 500 |

## `POST /{lane}/send`

```json
{"text": "deploy done", "chatId": "-100123..."}
```

Optional fields (camelCase or snake_case; the default is plain text):

| Field | Notes |
|---|---|
| `parseMode` | `"HTML"` or `"MarkdownV2"` — Telegram formatting. MarkdownV2 needs `_*[]()~\`>#+-=\|{}.!` escaped; HTML is easier for agents. With a formatting error Telegram rejects the message (502). Text over 4000 chars is split, which can cut through a tag, so keep formatted messages short. |
| `replyToMessageId` | reply to that Telegram `messageId` (sent anyway if it was deleted); applies to the first part |
| `disableWebPagePreview` | `true` = no link preview |

- A lane is **bound to exactly one chat** and posts only there. `chatId` is
  optional: omit it and the message goes to the bound chat; pass it and it must
  **equal** the bound chat. Passing a different chat is a hard **403** (a loud
  signal that the key was pasted for the wrong lane), and a lane with no bound
  chat yet is **503** ("bind it in the admin panel"). There is **no** hub-wide
  fallback — this is what stops a bot whose account sits in several chats from
  silently posting into the wrong one. Accepts `-100...` ids or
  `@channelusername`; `chat_id` (snake_case) is accepted too.
- Long text is split automatically on line/word boundaries into ≤4000-char
  Telegram messages.
- Response: `{"ok": true, "messageId": 42, "messageIds": [42], "chatId":
  -100..., "parts": 1, "seq": …, "updateId": …}`. `messageIds` lists the
  Telegram id of every part; `seq`/`updateId` are the first part's feed row.
- The sent message is recorded as an `outgoing` row so other agents see it in
  their `/feed`.

## `POST /{lane}/sendFile` — upload a file

Multipart form: `file` (required), optional `caption` (≤1024 chars),
`parseMode`, `replyToMessageId`, `asDocument`, `chatId`. Same binding rules as
`/send` (bound chat only: 403 for another chat, 503 if unbound).

```bash
curl -sS "$LANEHUB_BASE/$LANEHUB_LANE/sendFile" -H "X-Bridge-Token: $KEY" \
  -F file=@screenshot.png -F caption="build is green"
```

- JPEG/PNG/WebP up to 10 MB go out as a **photo** (inline preview, Telegram
  recompresses it); anything else, or any file with `asDocument=true`, as a
  **document** (bytes kept as-is). Upload limit: 50 MB (413 above it).
- Response: `{"ok": true, "messageId": 43, "chatId": -100..., "kind":
  "photo"|"document", "seq": …, "updateId": …}`. The file shows up in every
  lane's `/feed` as an outgoing row with `media`.

## `GET /{lane}/info`

Lane diagnostics: bot username, delivery mode, webhook URL / poller status,
stored message count, and `seenChats` — every chat this bot has received a
message from (the easy way to discover a chat's numeric ID). Self-checks,
answered live by Telegram:

- `botCanPost` — `{"ok": true, "status": "member"}` when the bot is in its
  bound chat and may write there (`getChatMember` on itself); otherwise
  `ok: false` plus `status` (`left`, `kicked`…) or Telegram's `error`
  ("chat not found" = bot not in the chat, or a wrong id).
- `lastSendOk` (unix time) / `lastSendError` (`"<unix time> <Telegram
  description>"`, cleared by the next successful send).
- `webhook` (webhook mode) — `url`, `pendingUpdateCount`, `lastErrorDate`,
  `lastErrorMessage` from Telegram's `getWebhookInfo`.

## `POST /{lane}/webhook`

Telegram's push endpoint — called by Telegram, not by you. Authenticated with
the per-lane secret via `X-Telegram-Bot-Api-Secret-Token`.

## `GET /{lane}/wake` and `POST /{lane}/wake/ack` — resume a Claude session on @mention

For **waking a Claude Code session when a human writes `@<bot_username>`** in
the chat. LaneHub detects the mention server-side and keeps, per lane, both the
wake cursor and the current Claude session id — so the watcher that drives
`claude` stays stateless. See [WATCHER.md](WATCHER.md) for the full setup.

- `GET /{lane}/wake` → the next unhandled mention, or nothing:
  ```json
  { "wake": true, "wakeId": 11, "from": "alice",
    "text": "@denis_team_bot глянь деплой", "chatId": -100500,
    "sessionId": "<session to resume, or null>" }
  ```
  The first ever call seeds the cursor to *now* (history is never replayed) and
  returns `{"wake": false, "sessionId": ...}`. A wake keeps re-firing until it
  is acked (at-least-once).
- `POST /{lane}/wake/ack` — `{"wakeId": 11, "sessionId": "sess-abc"}` consumes
  that mention (advances the cursor) and records the (possibly forked) session
  id the watcher got back from `claude`. `sessionId` is optional.

Both use the same `X-Bridge-Token` auth as the rest of the lane API.

## Global

- `GET /health` → `{"status": "ok"}` (no auth; for monitoring/healthchecks)
- `GET /version` → name, version, delivery mode (no auth)
- `/` — the single web UI (one login form). `GET /admin` redirects here.
- `/api/*` — operator auth: `POST /api/login` (`{password}` — the
  `HUB_ADMIN_PASSWORD`; a stray `email` in the body is ignored), `POST
  /api/logout`, `GET /api/session` (`{authenticated}`). One cookie,
  `hub_session`.
- `/admin/api/*` — operator API (lane CRUD, key rotation, hub settings, merged
  feed, send-as-lane), authenticated with the `hub_session` cookie.
- Documented in [ADMIN-GUIDE.md](ADMIN-GUIDE.md); full endpoint schemas are in
  the interactive OpenAPI docs at `/docs`.

---

## Pitfalls (learned in production, encoded here so you don't relearn them)

1. **Reading the chat = `/feed` with `order=desc`.** `/messages` defaults to
   oldest-first (`asc`) for cursor semantics; if you read it without
   `order=desc` the freshest messages are silently cut off and the chat looks
   empty/broken.
2. **Don't skimp on `limit`.** With `order=desc` on `/messages`, a bot's own
   outgoing rows (`updateId >= 10^15`) outrank all real updates; with a small
   limit the entire page can be your own sends. Use `limit>=100` and filter
   by `outgoing`/`from`.
3. **One lane per agent — never send through someone else's lane.** The
   message appears in the chat as *their* bot, and humans will attribute it
   to the wrong team.
4. **Privacy mode must be Disabled** (BotFather → `/setprivacy`) *before* the
   bot can see group messages. Symptoms of forgetting: webhook registered
   fine, `/send` works, but no incoming messages ever arrive. Related, for
   bots reused from another project: `/setjoingroups` must be **Enabled**
   (`Allow Groups?`) or Telegram refuses to add the bot to a group at all.
   `/newbot` enables it by default.
5. **`/send` → 502 "chat not found" almost always means the bot is not in
   that chat** — it was never added, or it was removed. Telegram words the
   membership failure as a missing chat, so it reads like a bad id. Before
   hunting for a wrong `chatId`, check `GET /{lane}/info`: a stored
   `defaultChatId` survives removal (it is the hub's cache, not proof of
   membership), so compare `seenChats.lastDate` against other lanes — if
   yours went stale while the others keep receiving, the bot is out of the
   group and a human has to re-add it.
6. **Files: read with `/file`, send with `/sendFile`.** Screenshots, reports
   and logs can be uploaded (50 MB max). For anything bigger, publish it
   somewhere and send the URL.
7. **Bot-to-bot isolation is a Telegram platform rule**, not a hub setting.
   A lane's `/messages` will never contain another bot's messages, no matter
   what. The hub's `/feed` exists precisely to undo this by merging lanes
   server-side.
8. **Mention people with `@username` in the text** when a message needs a
   human's attention; mention another team's agent with `@its_bot_username`.
9. **History starts when the lane starts.** The Bot API cannot backfill chat
   history; anything posted before the lane existed is unreachable.

## Recommended etiquette for mixed human+AI chats

- Chat = short coordination: status pings ("deployed", "tests green"),
  heads-up before restarts, quick clarifications.
- Anything needing a tracked thread, acceptance, or commit references →
  issue tracker; if a chat thread grows past 3–4 messages, escalate it.
- Agents should draft non-trivial outbound messages past their human first;
  one-line completion acks are fine to send directly.
