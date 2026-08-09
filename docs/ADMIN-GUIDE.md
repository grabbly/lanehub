# Admin guide — the panel and the member portal

Two web surfaces, one hub:

| URL | Who | Auth |
|---|---|---|
| `/admin` | the administrator | `HUB_ADMIN_PASSWORD` from `.env` |
| `/portal` | team members | email + password issued by an invitation |

The admin's day-to-day workflow is intentionally two actions: **set the
project chat once, invite people**. Everything else happens in the member
portal without the admin.

---

## `/admin` — sections

### Lanes

One card per lane (bot identity). On each card:

- **API key** — show / copy / **rotate** (old key dies instantly; hand keys
  over via DM or a secret manager, never through the group chat).
- **Endpoint** — the lane's base URL agents call.
- **Default chat** — where `/send` posts when the request has no `chatId`.
  ⚠️ Don't type chat ids by hand: a group id copied without its `-100` prefix
  (`4388659826` instead of `-1004388659826`) fails with "chat not found".
  After anyone posts in the group, the chat appears under **seen chats** —
  click the chip and the correct id is set for you.
- **agent recipes** — ready-made curl commands for that lane.
- **webhook status** (webhook mode) — live `getWebhookInfo`; `last_error`
  explains delivery problems.
- disable / delete (history is kept; only the lane and its key go away).

**Add lane** — manual lane creation: only the bot token is required; the
slug is auto-derived from the bot's username (`@denis_team_bot` → `denis_team`).
Prefer the invitation flow below — then you never touch this form.

**✉️ Teammate onboarding messages** — RU/EN copy-paste texts for teams that
skip the portal flow (member creates a bot and DMs you the token).

### Team

- **Project chat** — the group/channel id every new lane inherits (including
  lanes members create in the portal). Also the last-resort fallback at send
  time: explicit `chatId` → lane default → project chat.
- **📧 Email (SMTP)** — see [Invitation emails](#invitation-emails-smtp).
- **Members list** — each row: email, lane (if connected), last login,
  **re-issue invite** (new password, old one dies) and **remove** (the
  account goes away; their lane survives and is managed under Lanes).
- **Invite teammate** — enter an email (+ optional name). The hub creates
  the account with a generated password and:
  - SMTP configured → emails the invitation automatically;
  - otherwise → shows the credentials and a ready invite text to copy-paste
    into a DM. The password is shown **only in this dialog** (stored hashed);
    if it's lost, use re-issue.

### Feed

Live merged view of the whole chat (all lanes + humans, deduplicated), with
a chat filter and a send box — pick a lane, type, send. Useful for verifying
a new lane end-to-end without touching curl.

---

## `/portal` — what a member sees

1. **Login** with the emailed/DM'd credentials (interface auto-detects RU/EN,
   toggle in the header).
2. **No lane yet** → a 2-step guide: create a bot via @BotFather
   (`/newbot`, then `/setprivacy` → **Disable**; for a reused bot also
   `/setjoingroups` → **Enable**) and paste the token.
   The lane is created automatically: slug from the bot username, chat from
   the project chat setting.
3. **Lane view** (also what they see on every later visit):
   - bot + endpoint + **API key** (show / copy / rotate);
   - ready-to-run curl recipes **with the key already inlined**;
   - an **agent-prompt block** (for CLAUDE.md / system prompt) describing how
     to read the chat, post statuses, and behave — copy button included;
   - final-step reminder to add their bot to the team group;
   - password change.

Members can only ever see and manage their own lane.

---

## Invitation emails (SMTP)

Configure under **Team → 📧 Email (SMTP)**: host, port, user, password,
from-address, STARTTLS, then **Save SMTP** and **Send test email** to verify
before inviting anyone.

Rules:

- **Panel wins over environment.** `HUB_SMTP_*` env vars act as a fallback
  for infra-as-code setups; anything saved in the panel overrides them.
  Clearing the host in the panel falls back to env (or disables email).
- **The password is write-only.** The API returns only a `passwordSet` flag;
  leaving the password field blank on save keeps the stored one.
- Settings live in the hub database (`data/hub.db`) — the same file that
  holds bot tokens and API keys. Protect and back it up as a secrets store.
- Typical values: port 587 + STARTTLS on (most providers); Gmail needs an
  app password; a local relay may need no user/password at all.

Without SMTP nothing is blocked: invitations still work as copy-paste texts.

---

## Session & security notes

- Admin and member sessions are signed cookies (HMAC with a secret stored in
  the DB), valid 7 days; member passwords are stored as PBKDF2 hashes.
- Failed logins are throttled server-side.
- Always serve both surfaces over HTTPS ([INSTALL.md](INSTALL.md)) — the
  session cookie will not survive a cookie-stripping/plain-HTTP setup, and
  the login screen will tell you exactly that.
