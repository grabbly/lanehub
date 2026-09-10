# LaneHub — UX/UI design handoff

**The ask:** redesign the web UI to be genuinely well-crafted — clearer hierarchy,
calmer information density, a more meaningful and beautiful operator experience —
**without changing any behavior or the API**. The current UI is functional but
utilitarian (flat monochrome cards, terminal-ish key/value rows). Make it feel
like a considered product, not a debug panel.

The entire UI is one file: [`app/static/app.html`](../app/static/app.html)
(~750 lines: inline `<style>` + inline vanilla JS, no framework, no build step).
That is the deliverable to rewrite.

---

## What LaneHub is (so the design means something)

LaneHub is a **self-hosted, single-operator console** that bridges Telegram
chats and AI coding agents. Humans chat normally in a Telegram group; AI agents
(Claude Code, Codex, CI, cron — anything that can `curl`) read the whole
conversation and post back under their **own bot identity**.

Core objects:

- **Chat** — a Telegram group/channel. The primary unit of the UI.
- **Lane** — one Telegram bot + its own API key + its own HTTP endpoint
  (`/{slug}/send`, `/feed`, `/messages`), **bound to exactly one chat**. It
  posts only there — that binding is the safety property that stops a bot from
  writing into the wrong chat. A lane is "a bot in a chat".
- **Agent recipe** — the payoff artifact: a copy-paste block (API key + full
  API address + the bound chat id, all inlined) that the operator pastes into an
  agent's CLAUDE.md / system prompt. **This is the thing people come to copy.**

**The user** is a single technical operator (the person running the hub). They
manage a handful of bots across a few Telegram chats. This is a power-user admin
tool, not a consumer app — but "admin tool" is not an excuse for it to look
unloved. Think Linear/Vercel/Raycast-grade craft applied to an operator console:
dense but calm, fast, legible, confidence-inspiring at a glance.

Typical scale: a few chats, ~1–6 bots total. One bot can appear in two chats (as
two lanes) — e.g. the same `@somebot` bound to chat A and chat B.

---

## Hard constraints (do not break these)

1. **Single self-contained file.** Everything stays inline in `app/static/app.html`
   — one `<style>`, inline `<script>`, no separate CSS/JS assets. FastAPI serves
   this file directly (`FileResponse`); there is **no static-file mount and no
   build pipeline**, so extra asset files would 404. SVG icons must be inline.
2. **No external network dependencies.** The hub is self-hosted and may run
   air-gapped. **No CDN links, no web fonts, no remote scripts.** Use a system
   font stack and inline SVG. (The page currently does exactly this — keep it.)
3. **Preserve every behavior and the API contract.** Don't touch endpoints,
   payloads, field names, or what any control does. All the JS logic
   (`api()`, `loadLanes()`, `showRecipes()`, binding, rotate, feed polling, auth,
   copy-to-clipboard with its select-fallback) must keep working identically.
   You are re-skinning and re-laying-out, plus tasteful interaction polish — not
   changing what the app *does*.
4. **Light + dark parity.** Theming is already CSS-variable driven with a
   `@media (prefers-color-scheme: dark)` block (`--bg --panel --border --text
   --muted --accent --accent-text --danger --ok --chip`). Both themes must look
   deliberate. Extend the token set rather than hard-coding colors.
5. **Accessible & keyboard-friendly.** Real focus states, labelled controls,
   sufficient contrast (both themes), sensible tab order, `dialog` semantics
   preserved. The login `<form>` must stay a real submitting form (password
   managers rely on it).
6. **Responsive.** Must hold together from ~360px (phone) to desktop. The current
   layout is only lightly responsive — improve it.

**Non-goals:** no new features, no framework migration, no backend changes, no
routing changes. If a change would require touching Python, it's out of scope.

---

## Tech surface

- **File:** `app/static/app.html`. **Served at:** `GET /` (see `app/main.py`).
- **No dependencies.** Plain DOM APIs. `$ = id => document.getElementById(id)`.
- **Run it locally** (fake Telegram, no real tokens — from the README
  "Development" section):
  ```bash
  python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
  .venv/bin/uvicorn scripts.fake_telegram:app --port 8081 &
  HUB_ADMIN_PASSWORD=dev HUB_TELEGRAM_API=http://127.0.0.1:8081 \
    .venv/bin/uvicorn app.main:app --port 8090
  # open http://127.0.0.1:8090/  (sign in with password "dev")
  # seed data: POST /admin/api/lanes with {botToken, defaultChatId, title} after logging in;
  # give a chat a title by POSTing to http://127.0.0.1:8081/_push {token, from, chat_id, text}
  ```
- **API the UI calls** (all JSON; auth via an httpOnly `hub_session` cookie):
  - `POST /api/login {password}` · `POST /api/logout` · `GET /api/session`
    → `{version, authenticated, deliveryMode, publicBaseUrl, adminPasswordSet}`
  - `GET /admin/api/lanes` → `{lanes: [Lane]}` (see shape below)
  - `POST /admin/api/lanes {botToken, title?, slug?, defaultChatId?}` → Lane
  - `PATCH /admin/api/lanes/{slug} {title?|defaultChatId?|enabled?|operatorChatId?|botToken?}`
  - `POST /admin/api/lanes/{slug}/rotate-key` → `{apiKey}`
  - `DELETE /admin/api/lanes/{slug}`
  - `GET /admin/api/lanes/{slug}/webhook-info` · `GET /admin/api/lanes/{slug}/logs`
    → `{logs:[{ts,level,message}], ctxTokens}`
  - `GET /admin/api/feed?limit&order&chatId` → `{messages:[Msg], chats:[{chatId,title}]}`
  - `POST /admin/api/lanes/{slug}/send {text, chatId?}`
  - `GET /admin/api/settings` / `PATCH` → `{projectChatId, budget5hUsd, budgetWeekUsd}`
- **Lane shape:** `{slug, title, botUsername, apiKey, defaultChatId, enabled,
  deliveryMode ('webhook'|'polling'), operatorChatId, webhookUrl, polling(bool),
  storedMessages(int), seenChats:[{chatId,title,lastDate}]}`.
  `defaultChatId` empty string = **unbound** (bot can't post yet).
- **Msg shape:** `{lane, from, chatId, chatTitle, text, date, outgoing(bool),
  media}`.

---

## Current information architecture (what exists today)

- **Login** — single card, password only, hint "Enter HUB_ADMIN_PASSWORD".
- **Header** — logo `🛤️ LaneHub`, `v{version} · {deliveryMode}` meta, GitHub
  link, Log out. **Tabs:** Chats / Feed / Settings.
- **Chats tab** — grouped by chat:
  - one **chat block** per Telegram chat: header (`💬 {title}  {chatId}  N bots`),
    then the **bot cards** bound to it. Each bot card shows: API key
    (show/copy/rotate), Endpoint, Operator chat (input+save), and actions
    (enable/disable · webhook status · **agent recipes** · session log · delete).
  - a **"➕ Add a bot to this chat"** form inside each block (token → creates a
    lane pre-bound to that chat).
  - an **"⚠️ Bots without a chat"** block for unbound lanes (keeps the
    "seen chats" chips to bind them).
  - a top-level **"➕ Add a bot for a NEW chat"** disclosure with a 5-step guide.
- **Feed tab** — merged view of the whole conversation (all bots + humans,
  deduped), a chat filter dropdown, auto-refresh, and a send box (pick a lane,
  type, send as that bot).
- **Settings tab** — "Default chat (prefill)" only.
- **Dialogs (`<dialog>`):**
  - **Agent recipes** — the pinned CLAUDE.md block (key + API address + chat id
    inlined) + raw curl for read/send, with a RU/EN toggle. The hero artifact.
  - **Session log** — the watcher's activity for a bot, with a **context-window
    meter** (`{k} / 1M (%)`, color by fill) — a genuinely nice component to
    elevate.

---

## UX/UI critique — where it falls short (prioritized)

1. **Flat hierarchy.** Chat header, bot card, and nested rows all read at nearly
   the same visual weight. Nothing guides the eye. There's no clear "this is the
   chat / these are its bots / here's the one action you want".
2. **The payoff is buried.** "agent recipes" (the thing operators actually come
   to copy) is one small button among five. It should be the primary action on a
   bot, visually and hierarchically.
3. **State is illegible at a glance.** enabled/disabled, webhook vs polling,
   bound vs unbound, message count, "is this bot healthy / has it ever posted"
   are scattered tiny grey badges. An operator should read a bot's health in one
   glance (a status dot / clear affordance), especially **unbound** (can't post)
   which is the critical failure state.
4. **Card-in-card heaviness.** chat-block → bordered bot card → bordered rows is
   boxy. Needs a lighter, more typographic grouping (rhythm, whitespace,
   dividers) instead of nested borders.
5. **Key/value rows are terminal-ish.** Long rows of `label  input  button  hint`
   with no rhythm. Could be a cleaner definition layout or inline-editable fields.
6. **Feed looks like a log, not a conversation.** It's the "whole chat" — it
   could feel like one (sender identity, bot vs human distinction, time grouping,
   attachments) while staying dense.
7. **Empty/first-run states are bare** ("No bots yet — …"). First run is a chance
   to teach the chat→bot→bind→copy flow with a proper illustrated/■stepped state.
8. **Micro-interactions are minimal.** Copy feedback is a text swap; no toasts,
   no transitions, no skeleton/loading states, no optimistic feel. Rotate/delete
   use `confirm()`. Destructive actions deserve better affordances.
9. **Login is plain.** First impression of the product; could carry the brand
   and set the tone with near-zero cost.
10. **Iconography is emoji-only.** Fine as a fallback, but a small inline-SVG icon
    set would lift the whole thing (chat, bot, key, bound/unbound, webhook,
    copy, rotate, delete).

---

## Design goals

- **Operator confidence at a glance** — which bot posts to which chat, and is it
  healthy? Make binding and health status unmistakable.
- **Put the recipe front and center** — copying the pinned instruction is the
  core loop; make it a first-class, delightful action.
- **Calm density** — lots of technical facts, presented with hierarchy,
  whitespace and rhythm so it never feels like a form dump.
- **One coherent system** — a real type scale, spacing scale, elevation, and an
  extended token palette that reads as deliberate in both light and dark.
- **Fast & tactile** — meaningful focus/hover/active states, smooth (but quick)
  transitions, good copy/toast feedback, graceful loading/empty/error states.
- **Solid responsive** from phone to desktop.

## Suggested opportunities (not prescriptive)

- Chats tab: chat as a clear section with a health summary; bots as lighter rows
  with a status dot + a prominent "Copy agent setup" primary action and a
  secondary menu (rotate/logs/webhook/delete). Make the unbound state loud.
- A proper **bot detail / recipe** experience (the current dialog is the seed):
  key management, the pinned block with obvious copy, live-ish health.
- Feed as a readable conversation lane with a persistent composer.
- A cohesive icon set (inline SVG), a refined header, a branded login.
- Nice-to-have: a subtle keyboard palette / shortcuts, given the power-user
  audience.

## Definition of done

- `app/static/app.html` still a single self-contained file, zero external deps,
  every existing control works exactly as before against the same API.
- Light and dark both look intentional; passes basic a11y (focus, contrast,
  labels); responsive 360px→desktop.
- Verify by running locally (recipe above), seeding a couple of chats with a
  couple of bots plus one unbound bot, and exercising: login, view chats,
  copy a recipe, rotate a key, bind an unbound bot, open the feed, send a
  message, open a session log. No console errors.

## Gotchas

- The **agent recipes** and **session log** dialogs contain real, careful copy
  and the context-window meter — preserve their content and logic; restyle only.
- Copy-to-clipboard has a deliberate `select()`-and-fallback path for
  non-secure contexts — keep it.
- `defaultChatId === ''` is the unbound sentinel; a lane with it must not offer
  a working recipe (today the recipes button is disabled / warns).
- The same bot (same `botUsername`) can legitimately be two lanes in two chats.
- Deletions/rotations call `loadLanes()` to refresh — keep the refresh contract.
