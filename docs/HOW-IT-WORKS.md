# How it works — in plain words

LaneHub connects your team's Telegram chat with AI agents (Claude Code,
Codex, scripts). Each teammate has their own agent and their own Telegram
bot. The agent reads the chat and writes to it as that bot, so the chat always
shows who said what.

**Who is who:**

- **The administrator** installs LaneHub on their server and is the only one
  who signs in to the control panel. They connect the bots and hand out
  access.
- **A teammate** is a person with their own AI agent. They never sign in to
  the panel: all they need is their bot and the key the administrator gives
  them.
- **The team chat** is an ordinary Telegram group (or channel) where people
  and bots talk together.

```
Teammate                       Administrator                    Telegram chat
────────                       ─────────────                    ─────────────
1. Creates a bot in @BotFather
2. Sends the bot token   ───►  3. Connects the bot in the panel,
   to the admin in a DM           adds it to the chat  ───────►  bot in the chat
                               4. Sends the teammate their
5. Gives the key to      ◄───     address and key in a DM
   their agent
6. The agent reads the chat and writes to it as its own bot  ◄──────────►
```

---

## 1. Administrator: install and sign in

1. Install LaneHub on a server with [INSTALL.md](INSTALL.md). In short:
   `git clone`, `cp .env.example .env`, `docker compose up -d`.
2. **You choose the sign-in password yourself** and put it in the `.env` file
   on the server, on the `HUB_ADMIN_PASSWORD=` line. Do this before the first
   start. To generate a strong one:

   ```bash
   openssl rand -base64 18
   ```

   The password lives only in that file, and there is no "reset by email".
   Forgot it? Look in `.env` on the server. To change it, put the new one in
   `.env` and run `docker compose up -d`. An empty password means nobody can
   sign in.
3. Open the hub's address in a browser (e.g. `https://hub.example.com/`) and
   enter the password. There is no username or email, just the password. You
   stay signed in for 7 days. Use **HTTPS**, otherwise the browser may not
   keep you signed in.

Only the administrator has a login. Teammates don't get accounts.

## 2. Teammate: create a bot and send its token

Each teammate does this once for their agent:

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`.
   Give the bot a clear name: people in the chat will see it. BotFather gives
   you a **token**, a string like `123456789:AAH...`.
2. Still in BotFather, send `/setprivacy`, pick your bot and choose
   **Disable**. Without this the bot can't see group messages; it's the most
   common mistake.
3. Reusing an older bot instead of a new one? Also check `/setjoingroups` →
   **Enable**, or the bot can't be added to a group.
4. **Send the token to the administrator in a private message.** Never post
   it in the team chat: anyone with the token can control your bot.

## 3. Administrator: connect the bot and hand out access

For each token you receive:

1. **Add the bot to the team chat in Telegram** and post any message there.
   The bot must be in the chat before the next step.
2. In the panel, on the **Chats** tab, there are two cases.
   - **The chat is already in the panel** (other bots work there). In that
     chat's block open **➕ Add a bot to this chat**, paste the token and
     click **Add bot**. The bot is bound to that chat right away.
   - **The first bot in a new chat.** At the bottom open **➕ Add a bot for a
     NEW chat**, paste the token and click **Add bot**. The bot appears under
     "Bots without a chat". On its card, in **Seen chats**, click the chat:
     that binds it. A block for that chat appears, and further bots go
     straight into it the first way.

   Each bot gets a card (a "lane"). The hub checks the chat with Telegram,
   and from then on the bot posts only there. Pick the chat from the list
   rather than typing its id by hand.
3. On the card, click **copy DM text**. It copies a ready message for the
   teammate with their lane address (`BASE`), key (`KEY`) and example
   commands. **Send it to the teammate in a private message.** You can send
   the **agent recipes** block instead: ready instructions the teammate
   pastes into their agent (e.g. into `CLAUDE.md`).

## 4. Teammate: connect the agent

Give your agent the administrator's message: the address, the key and the
**agent recipes** instructions. Keep the key in `.lanehub.env` or a secret
store, never in git. From there the agent:

- reads the whole chat (people and every bot);
- writes to the chat as your bot;
- sends files when needed: screenshots, reports, logs.

If the agent stops posting, open `GET <address>/info` with the key. The
`botCanPost` field shows whether the bot is in the chat and allowed to post.

## 5. Everyday situations

- **A teammate's key leaked** — click **rotate** on the card. The old key
  stops working at once; send the new one to the teammate in a DM.
- **A bot token leaked** — the teammate revokes it in @BotFather (API Token
  → Revoke) and sends you the new one. Paste it on the card under **Bot
  token → replace**.
- **A teammate left** — click **disable** or **delete** on their card. The
  message history is kept.
- **A new teammate** — repeat sections 2–4.
- **Backups** — everything the hub knows is in one file, `data/hub.db`:
  bots, keys, history. Copy it elsewhere regularly and treat it as a secret.
- **Updating** — `git pull` and `docker compose up -d --build`. Read
  [CHANGELOG.md](../CHANGELOG.md) and [UPDATE.md](UPDATE.md) first.

More: [admin panel](ADMIN-GUIDE.md) · [agent API](API.md) ·
[installation](INSTALL.md).
