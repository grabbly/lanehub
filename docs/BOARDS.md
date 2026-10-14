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
| Client | secret link | read client-visible cards, prices and the ledger header |

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

## Agent API

All calls take `X-Bridge-Token: <lane key>`. The board is the one of the lane's
bound chat (the oldest one if the chat has several; pick another with
`?board=<slug>`).

```bash
# the whole board: settings, ledger numbers, column sums, cards
curl -sS -H "X-Bridge-Token: $KEY" "$HUB/$LANE/board"

# new card (title in at least one language)
curl -sS -X POST -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"titleI18n":{"uk":"Нова задача"},"status":"backlog","pay":"estimate","price":40,"source":"#71"}' \
  "$HUB/$LANE/board/tasks"

# move / edit: send only what changes; pass "version" to refuse a stale edit (409)
curl -sS -X PATCH -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"status":"doing","version":3}' "$HUB/$LANE/board/tasks/<id>"

# what changed since event N
curl -sS -H "X-Bridge-Token: $KEY" "$HUB/$LANE/board/events?since=N"
```

Card fields: `status`, `position`, `grp`, `source`, `sourceMessageId`, `pay`,
`price`, `hours`, `clientVisible`, `titleI18n`, `clientNoteI18n`,
`internalNote`. Language maps accept only `en`, `pl`, `ru`, `uk`. A 409 carries
the current card in `task`: re-read, re-apply, retry.

## Operator API

Cookie session, under `/admin/api/boards`: list/create boards, `GET/PATCH/DELETE
/{id}`, `POST /{id}/rotate-client-token`, cards (`/{id}/tasks…`, including
DELETE), ledger (`/{id}/ledger…`), `/{id}/events`, and `POST /{id}/import`
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
