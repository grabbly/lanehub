# LaneHub Messenger reader

A browser extension that connects one **Facebook Messenger group chat** to a LaneHub lane.
Meta gives bots no way into Messenger group chats, so the extension does the bot's reading
instead:

- It runs in a browser signed in to Messenger as the **bot's own account** (a separate account
  that is a member of the group).
- It reads the group's text, voice messages and photos and sends them to the hub, so agents see
  them in `/feed` and the watcher wakes on `@<bot account name>`. Photos go up at full size, and
  `./tg-file.sh <fileId>` downloads them.
- It types the hub's replies (drafts) into the message box. **A person presses Send.** The
  extension never sends anything itself.

Supported: Safari on macOS now. Chrome loads the same folder unpacked.

## 1. Create the lane in the hub

Admin panel → **＋ Messenger group**: the bot account's name as Messenger shows it, and the
group's link (`https://www.messenger.com/t/<id>`). Copy the lane's API key from its card.

## 2. Install in Safari (macOS)

1. Build the wrapper app: open `extensions/safari/LaneHub Messenger/LaneHub Messenger.xcodeproj`
   in Xcode and press Run, or from the repo root:

   ```bash
   xcodebuild -project "extensions/safari/LaneHub Messenger/LaneHub Messenger.xcodeproj" -scheme "LaneHub Messenger" -configuration Release CODE_SIGN_IDENTITY="-" build
   ```

   To keep the extension after Safari restarts, sign it with your Apple developer team instead
   (needs an "Apple Development" certificate in the keychain), and copy the app to
   `~/Applications`:

   ```bash
   xcodebuild -project "extensions/safari/LaneHub Messenger/LaneHub Messenger.xcodeproj" -scheme "LaneHub Messenger" -configuration Release DEVELOPMENT_TEAM=<your-team-id> CODE_SIGN_STYLE=Automatic -allowProvisioningUpdates build
   ```

2. Unsigned build only: Safari → Settings → Advanced → **Show features for web developers**. Then
   Develop → **Allow Unsigned Extensions**. Safari turns this off on every restart, and the
   extension disappears from the list until you allow it again.
3. Start the built **LaneHub Messenger** app once, then Safari → Settings → Extensions → enable
   **LaneHub Messenger reader** and allow it on `www.messenger.com` and your hub's address.
4. Click the extension's toolbar icon → **Settings**: hub address, lane name, lane API key →
   **Save and check**.

The Xcode project references the files in `extensions/messenger/` directly. After editing them,
build again and reload the extension.

## 3. Install in Chrome

1. Get the files: `git clone` the LaneHub repository (or `git pull` in your copy).
2. Open `chrome://extensions` and switch on **Developer mode** (top right).
3. **Load unpacked** → pick the `extensions/messenger/` folder. The card shows **LaneHub
   Messenger reader** and its version.
4. Pin it (puzzle icon → pin), click its icon → **Settings**: hub address, lane name, lane API
   key → **Save and check**. Chrome asks to allow the hub's address; allow it.
5. In the same Chrome profile, sign in to messenger.com as the bot account and open the group.

## Updating

The version is on the extension's card (`chrome://extensions`) and in `manifest.json`.

- **Chrome:** `git pull` in the repository, then `chrome://extensions` → **Reload** (the circular
  arrow) on LaneHub Messenger reader, then reload the Messenger tab. Settings are kept.
- **Safari:** `git pull`, build the wrapper app again (step 2.1), start it once, and reload the
  Messenger tab.

To check the new version is running: the label in the bottom-left corner of the group page shows
**reading · N on screen**, and a photo posted in the group shows up in `/feed` with
`media.kind = "photo"` within a few seconds.

## Using it

Open the group (`messenger.com/t/<id>`) in that browser, signed in as the bot account, and leave
the tab open. A small **LaneHub** label in the bottom-left corner shows what it is doing (only you
see it):

- **reading · N on screen** — messages are going to the hub.
- **draft in the box — press Enter to send** — a reply from the hub is in the message box. Read
  it, edit it if needed, and send it, or clear the box to drop it.
- **draft waiting — clear the message box** — you are typing yourself. The extension never types
  over that.
- Sending something of your own in the group closes the oldest open draft (or the one your
  message resembles), so a draft you answered in your own words is not typed afterwards.
- To drop a draft, clear the message box: after a few seconds it is closed as **dismissed** and
  is never typed again. A draft is typed only once in this browser, even after a reload.
- **fetching older photos · N left** — the hub has photo messages from the last 14 days without
  their files (for example from before 0.2.0). The extension scrolls the chat up to read them
  again and scrolls back down when done (**older photos fetched**). Leave the tab alone meanwhile.
  It tries this at most once a day while the number stays the same.
- **photo: …** — a photo could not be taken (the reason follows). The message still reaches the
  hub, as `[photo]` without a file. **photo: only a preview was found on the page** — the
  full-size link was not found, so the smaller picture shown in the chat went up instead.

Only the tab with the lane's group is read. Other chats in the same browser are ignored.

## Limits and risks

- Messenger's web page changes without notice. Everything that depends on its markup is in
  `parser.js` and `page.js`. When reading stops, those files need an update. If photos arrive as
  previews or not at all, open the group, select a photo message in DevTools → Elements, and run
  `lanehubPhotoDebug($0)` in the console (page context, not the extension's): it lists the
  attachment props `page.js` sees.
- Running a second account as a bot is against Facebook's terms. The account can be restricted
  or blocked. Tell the group members that this account is a bot and that its messages pass
  through LaneHub.
- Text, voice and photos. Videos and other files are not read yet (they show as `[attachment]` or
  `[photo]` without a file), and the hub cannot send files to a Messenger lane.
- A photo message waits up to 8 seconds for its full-size link before it goes to the hub; the
  messages below it wait with it, so the order stays right.
- Photos already in the hub before 0.2.0 (rows that say `[photo]` with no file) are fetched by
  scrolling back (see **fetching older photos** above), up to 14 days. Each gets a new `[photo]`
  row with the file; the old row stays as it was. Older ones: scroll up to them by hand.
