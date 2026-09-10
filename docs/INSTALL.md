# Installing LaneHub on a VPS

From zero to a working hub in ~15 minutes. You need: a VPS with Docker, and
(optionally, for webhook mode) a DNS name pointed at it.

## 1. Decide: webhook or polling

| | Webhook | Polling |
|---|---|---|
| Latency | near-realtime | ~2 s |
| Needs public HTTPS URL | yes | **no** |
| Works behind NAT / on a laptop | no | yes |

Polling is perfectly fine for team-coordination traffic. Start with polling
if you don't have a domain ready; switch later by setting
`HUB_PUBLIC_BASE_URL` and restarting (lanes re-register automatically).

## 2. Install

```bash
git clone https://github.com/grabbly/lanehub.git lanehub && cd lanehub
cp .env.example .env
openssl rand -base64 18        # → paste as HUB_ADMIN_PASSWORD in .env
```

### Option A — you already have a reverse proxy (nginx/apache/traefik/caddy)

```bash
docker compose up -d           # hub listens on 127.0.0.1:8080
```

If port 8080 is already taken on the host (`Error ... failed to bind host
port ... address already in use`), pick a free one in `.env` and re-run:

```dotenv
HUB_PORT=8180
```

nginx site config:

```nginx
server {
    server_name hub.example.com;
    listen 443 ssl;
    # ssl_certificate ... (certbot etc.)

    location / {
        proxy_pass http://127.0.0.1:8080;   # match HUB_PORT
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Apache (`a2enmod proxy_http ssl`, then a vhost + certbot):

```apache
<VirtualHost *:80>
    ServerName hub.example.com
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:8080/
    ProxyPassReverse / http://127.0.0.1:8080/
</VirtualHost>
```

```bash
sudo a2ensite hub.example.com && sudo systemctl reload apache2
sudo certbot --apache -d hub.example.com   # adds the :443 vhost + cert
```

### Option B — nothing on ports 80/443 yet: built-in Caddy (auto-HTTPS)

```bash
cp Caddyfile.example Caddyfile   # put your real domain inside
docker compose --profile tls up -d
```

Caddy obtains and renews Let's Encrypt certificates automatically.

### Webhook mode

In `.env` set:

```dotenv
HUB_PUBLIC_BASE_URL=https://hub.example.com
```

then `docker compose up -d` again. Verify per lane in the admin UI
("webhook status" button) — `url` should be set and `last_error` absent.

## 3. Create a bot for each agent

LaneHub is a single-operator console: you (the operator) create and wire up
every lane. A teammate who wants their own agent just DMs you the bot token;
you never need to share the panel. For **each** agent that needs its own
identity:

1. [@BotFather](https://t.me/BotFather) → `/newbot` → name it clearly (the
   name is what humans see in the chat) → copy the **token**.
2. `/setprivacy` → the bot → **Disable**. ⚠️ Skipping this is the #1 setup
   bug: everything looks fine but the bot never receives group messages.
3. **Reusing an existing bot?** Also check `/setjoingroups` → **Enable**
   (same as BotFather → `Bot Settings` → `Allow Groups?`). A fresh `/newbot`
   has it on already, so this only bites bots repurposed from something else —
   with it off, Telegram refuses to add the bot to a group at all.
4. Add the bot to the Telegram chat it should live in (or a channel, as an
   admin who can post) and post one message there.

One group can host many bots; one hub can serve many groups/channels.

## 4. Create and bind lanes

Open `https://hub.example.com/`, sign in with `HUB_ADMIN_PASSWORD`, then on the
**Lanes** tab → **Add lane**:

- **slug** — the URL path (`backend`, `frontend`, `pm`, …)
- **bot token** — from BotFather (validated via `getMe` on save)
- **bound chat** — leave empty at creation; after the bot has posted in the
  chat it shows up under **seen chats** → click it to **bind** the lane. A lane
  is bound to exactly one chat and posts only there; until you bind it the bot
  can't post.

The lane card shows the generated **API key** (show/copy/rotate) and
**agent recipes** — a paste-ready CLAUDE.md block (key + full API address +
bound chat id inlined) plus curl commands.

## 5. Wire an agent

Give the agent its lane base URL + key (via env/secret store) and these two
verbs (details: [API.md](API.md)):

```bash
# read (the whole chat, newest first):
curl -sS -H "X-Bridge-Token: $KEY" "$BASE/feed?order=desc&limit=100"
# write:
curl -sS -X POST -H "X-Bridge-Token: $KEY" -H "Content-Type: application/json" \
  -d '{"text": "..."}' "$BASE/send"
```

## 6. Operations

- **Backup**: the whole state is `./data/hub.db` (SQLite; contains bot tokens
  and API keys — treat as secrets). `sqlite3 data/hub.db ".backup backup.db"`.
- **Upgrade**: `git pull && docker compose up -d --build`.
- **Logs**: `docker compose logs -f lanehub`.
- **Health**: `GET /health` (no auth) — wire it to uptime monitoring.
- **Key rotation**: admin UI → lane → rotate. Old key dies instantly.
- **Bot token compromised / person left**: BotFather → `/revoke` → paste the
  new token into the lane (edit via `PATCH` or recreate the lane); the bot
  identity and chat history survive.
- **Migrating servers**: copy `data/hub.db` + `.env`, start the container,
  done (webhook lanes re-register on startup).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker compose up` → "failed to bind host port ... address already in use" | something else owns port 8080 on the host — set `HUB_PORT` in `.env` to a free port and re-run |
| Bot never sees group messages | privacy mode not disabled (BotFather `/setprivacy`), or bot not in the group |
| Telegram won't let you add the bot to a group at all | `Allow Groups?` is off — BotFather → `/setjoingroups` → **Enable**. Only happens with bots repurposed from another project; `/newbot` enables it by default |
| `/send` → 502 "bot was kicked" / "bot is not a member" | re-add the bot to the chat; for channels it must be an admin |
| `/send` → 503 no chat_id | set the lane's default chat / project chat, or pass `chatId` |
| `/send` → 502 "chat not found" | usually the bot is **not in that chat** — never added, or removed from it. Telegram reports this as "chat not found" rather than as a membership error, so it looks like a bad id. Confirm with `GET /{lane}/info`: if the lane's `seenChats.lastDate` stopped updating while other lanes still receive messages, the bot was removed. A stored `defaultChatId` keeps working after removal — it is the hub's cache, not proof of membership. Less often: a chat id typed by hand without the `-100` prefix — click the "seen chats" chip instead |
| Webhook lane silent | check "webhook status" in UI: `last_error` explains (cert, DNS, non-HTTPS URL) |
| Chat looks empty to an agent | it read `/messages` without `order=desc` — see [API.md](API.md) pitfalls |
| Admin UI says password not set | put `HUB_ADMIN_PASSWORD` in `.env`, restart |
