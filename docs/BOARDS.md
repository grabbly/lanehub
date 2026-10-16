# Boards — a task board per chat, with a read-only client page

Optional module (off by default). A board belongs to one Telegram chat: the
team and its agents keep the work on it, and the client follows along on a
secret read-only link, in their own language.

Enable it with `HUB_BOARDS=1` in `.env` and restart. With the flag off none of
the routes below exist (404) and nothing is shown in the panel.

## What a board holds

- **Five columns**: `backlog` → `queue` → `doing` → `waiting` (on the client) → `done`.
- **Cards**: title and a note for the client in up to four languages
  (EN / PL / RU / UK), price, payment type, optional hours, group (e.g. "TZ-2"),
  source (e.g. "#69"), an internal note, and "visible to client".
- **Payment types**: `estimate` (price not agreed yet), `deposit` (paid from the
  prepayment), `paid`, `rework` (free fix of our own work), `bonus` (free extra),
  `separate` (quoted separately).
- **Ledger**: payments, deposits and refunds. The header numbers are always
  derived, never stored: *allocated* = sum of `deposit` card prices,
  *remaining* = deposits − refunds − allocated.
- **Settings**: team language (internal default), client language (client page
  default), currency, hourly rate (hours = price ÷ rate when you need them).

Every change is written to an audit trail (`board_events`): who (`admin` or the
lane slug), what, when.

## Who can do what

| Role | How | Can |
|---|---|---|
| Operator | panel sign-in | everything: boards, cards, ledger, settings, client link |
| Agent | lane key (`X-Bridge-Token`) | read the board of **its own chat**, create cards, edit/move cards |
| Client | secret link | read client-visible cards, prices, ledger header; tick "✓ Done" on waiting cards |

Agents can't delete cards, touch the ledger or settings, or see the client
link. A board of another chat answers 404 to them, as if it didn't exist.

## The client link

`https://<hub>/<board-slug>/b/<token>/` — no login. The token is shown once
(on create and on rotate) and only its hash is stored, so the panel can't show
it again: copy it when it appears. **Rotate** issues a new link; the old one
stops working at once.

The page is `noindex`, sends no referrer, is never cached, and a wrong slug or
token gets the same 404 as a missing board. It shows only client-visible cards
with their client title/note, status, payment type and price — never internal
notes, hours, groups, sources or who changed what. The client switches language
at the top; text that isn't translated yet falls back to the board's client
language.

The page loads its data from the relative `data.json`, and the short form of
the link redirects relatively, so a reverse proxy can map an older address onto
it. Example (Nginx Proxy Manager → Advanced) keeping an existing link
`https://clients.example.com/yl/<token>/` alive:

```nginx
location /yl/ {
    rewrite ^/yl/(?!b/)(.*)$ /yl/b/$1 break;
    proxy_pass http://lanehub:8080;
}
```

### Client "✓ Done" tick

On the client page, cards in `waiting` ("we are waiting on you") are shown only in the «Потрібно від вас» banner at the top — there is no separate waiting column there (the operator panel still shows all five columns). Each of them gets a "✓ Зроблено / Done" button with an optional client comment and an explanatory hint line underneath. The client presses it when they did their part. It does **not** move the card and changes nothing else — it is a signal for the team and bots in case they forget to move the card. The client can add or edit their comment later, or untick the card if pressed by mistake ("Скасувати позначку"). No notifications of any kind are sent (no Telegram messages).

- **Endpoint**: `POST /{board_slug}/b/{token}/tasks/{task_id}/done` with body `{"done": true, "comment"?: string}` or `{"done": false}` → `{id, clientDoneAt, clientDoneComment}`. Extra fields or providing `comment` with `done: false` returns 422. Comments are plain text up to 500 characters (longer returns 422). Body size limit is 4 KB (returns 413).
- **Edit comment**: `done: true` on an already-done card replaces `clientDoneComment` (omitting `comment` leaves it unchanged; empty string clears it) while keeping the original `clientDoneAt`.
- Strict token verification (same constant-time check and identical 404 response for wrong slug/token or task of another board / not client-visible / archived / not in `waiting`).
- The client page calls the relative URL `tasks/<id>/done` so it works seamlessly behind reverse proxies mapping custom prefixes (e.g. `/yl/<token>/`).
- In-memory rate limiting per board token: max 30 writes per 10 minutes (returns 429).
- **Reset rule**: when a task leaves `waiting` (status changes), both `client_done_at` and `client_done_comment` are cleared automatically (the signal was consumed). Moving back to `waiting` starts clean.
- **Events**: records `client_done` (including comment payload if provided), `client_comment` (on comment edits), and `client_undone` with actor `client`.

## Rules for agents

Boards are **not** an internal team kanban or a dump of subtasks. A board is the **client's window**: where we are, what is done, what we need from the client, and prices. Agents keep it tidy together with their developer:

1. **One card = one result or request the CLIENT understands.** Do not post subtasks, technical chores, or internal steps. Keep those in your own tools, scratchpads, or commit messages.
2. **Write titles in the client language.** At least provide the client's language in `titleI18n`.
3. **Need something from the client?** Put what you need in the client note (`clientNoteI18n`) and move the card to `waiting`.
4. **Finished work?** Move the card to `done` only when the client can actually see or test the result.
5. **Archive done cards once seen:** Archive cards (e.g. after a week) with `PATCH /{lane}/board/tasks/<id>` `{"archived": true}`.
6. **Keep «Де ми зараз» current:** Update the status line after each meaningful step using `PUT /{lane}/board/status` with `statusI18n`.
7. **Check `clientDoneAt` and `clientDoneComment` on waiting cards:** When set, verify that the client did their part, read their comment if they left one, and move the card (or ask the developer). Agents can clear it via `PATCH /{lane}/board/tasks/<id>` with `{"clientDone": false}`, but cannot set it or modify `clientDoneComment` on the client's behalf.

## Agent API

All calls take `X-Bridge-Token: <lane key>`. The board is the one of the lane's
bound chat (the oldest one if the chat has several; pick another with
`?board=<slug>`).

```bash
# the whole board: settings, ledger numbers, column sums, non-archived cards
curl -sS -H "X-Bridge-Token: $KEY" "$HUB/$LANE/board"

# list cards: default = non-archived; ?archived=1 = only archived; ?archived=all = both
curl -sS -H "X-Bridge-Token: $KEY" "$HUB/$LANE/board?archived=1"

# update status line «Де ми зараз» (max 300 chars per language; empty map clears it)
curl -sS -X PUT -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"statusI18n":{"uk":"Налаштовуємо платіжний шлюз","en":"Setting up payment gateway"}}' \
  "$HUB/$LANE/board/status"

# new card (title in at least one language)
curl -sS -X POST -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"titleI18n":{"uk":"Нова задача"},"status":"backlog","pay":"estimate","price":40,"source":"#71"}' \
  "$HUB/$LANE/board/tasks"

# move / edit: send only what changes; pass "version" to refuse a stale edit (409)
curl -sS -X PATCH -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"status":"doing","version":3}' "$HUB/$LANE/board/tasks/<id>"

# clear client done mark: agents may clear (clientDone: false), but cannot set it
curl -sS -X PATCH -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"clientDone":false,"version":3}' "$HUB/$LANE/board/tasks/<id>"

# archive or unarchive a card (writes an archived/unarchived event)
curl -sS -X PATCH -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"archived":true,"version":3}' "$HUB/$LANE/board/tasks/<id>"

# what changed since event N
curl -sS -H "X-Bridge-Token: $KEY" "$HUB/$LANE/board/events?since=N"
```

Card fields: `status`, `position`, `grp`, `source`, `sourceMessageId`, `pay`,
`price`, `hours`, `clientVisible`, `titleI18n`, `clientNoteI18n`,
`internalNote`, `archived` (`archivedAt` in output), `clientDoneAt`. Language maps accept only `en`, `pl`, `ru`, `uk`. A 409 carries
the current card in `task`: re-read, re-apply, retry.

Derived ledger numbers (`allocated`, `remaining`) and column sums continue counting archived tasks (money does not disappear when a card is archived).

## Client page data (`data.json`)

The client view at `/{slug}/b/{token}/data.json` returns:
- `status`: current status line resolved for the viewer's language (fallback: lang → client_lang → any)
- `statusUpdatedAt`: timestamp when the status was last changed (or null)
- `tasks`: client-visible non-archived tasks, including `updatedAt` timestamp (used for the «нове» badge); tasks in `waiting` also include `id` and `clientDoneAt`
- `archive`: list of months, newest first, each `{month: "YYYY-MM", count, sum, items: [{title, price, pay}]}` (grouped by the month of `archived_at`; `sum` calculates deposit/paid cards).
- `depositHistory`: what the deposit was spent on, newest first (see below).

Strict privacy rules apply: internal notes, hours, groups, sources, and task IDs on non-waiting tasks are never exposed in `data.json`.

### «Куди пішов депозит» (deposit history)

On the client page the «Розподілено» and «Залишок» tiles are buttons (dotted
underline; hover shows a hint). A click or tap opens a small dialog listing
where the deposit went, and its footer reads «Депозит X − Розподілено Y =
Залишок Z» — the same numbers as the tiles. It closes with ✕, Esc or a click
outside, keeps keyboard focus inside while open and returns it to the tile.
The tiles stay plain when a board has no deposit activity.

`depositHistory` items, newest first (by date; on the same day spending is
listed above the top-up):

| `kind` | Fields | From |
|---|---|---|
| `deposit` | `amount`, `date` | ledger deposit entry (+) |
| `refund` | `amount`, `date` | ledger refund entry (−) |
| `task` | `title`, `status`, `amount`, `date` | a client-visible card with `pay: "deposit"`, archived ones included; `date` = archive date, else last change (`YYYY-MM-DD`, UTC) |
| `other` | `amount` | the sum of `pay: "deposit"` cards hidden from the client (`clientVisible: false`), one line, last |

Nothing else is exposed: no ids, no ledger notes, no internal notes or hours.
Hidden cards are counted in *allocated* (as before) but never named, so the
list still adds up: deposits − refunds = *deposit*, cards + «Інші роботи» =
*allocated*. Both the tiles and the list are computed from the same rows in one
read. The list is newest-first without a running balance: a card's date moves
when it is edited and hidden cards have no date, so a running balance would
jump around; the footer shows the one balance that matters.

## Operator API

Cookie session, under `/admin/api/boards`: list/create boards, `GET/PATCH/DELETE
/{id}` (PATCH accepts `statusI18n`), `POST /{id}/rotate-client-token`, cards (`/{id}/tasks…`, including
DELETE, PATCH with `archived: true|false`, `?archived=1|all`), ledger (`/{id}/ledger…`), `/{id}/events`, and `POST /{id}/import`
(with `dryRun`). The panel's **Boards** tab is built on these.

## Importing a board

`scripts/import_board.py` loads cards from a directory of JSON files (one card
per file; the file name is the card id) or from a single JSON file, and can
create the board, add ledger entries and keep an existing client link:

```bash
python scripts/import_board.py --db data/hub.db --board yl --create \
  --chat-id -1001234567890 --title "Client · example.com" \
  --team-lang ru --client-lang uk --rate 40 --currency zł \
  --ledger "payment:550:2026-10-13:stage 1" --ledger "deposit:450:2026-10-13" \
  --client-token-stdin --dry-run ./export/tasks < old-token.txt
```

Drop `--dry-run` to write. Re-running updates cards with the same id instead of
duplicating them. Back up `data/hub.db` first.

