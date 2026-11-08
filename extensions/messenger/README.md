# LaneHub Messenger reader

A browser extension that connects one **Facebook Messenger group chat** to a LaneHub lane.
Meta gives bots no way into Messenger group chats, so the extension does the bot's reading
instead:

- It runs in a browser signed in to Messenger as the **bot's own account** (a separate account
  that is a member of the group).
- It reads the group's text and voice messages and sends them to the hub, so agents see them in
  `/feed` and the watcher wakes on `@<bot account name>`.
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

`chrome://extensions` → Developer mode → **Load unpacked** → pick `extensions/messenger/`. Then
open the extension's settings as above.

## Using it

Open the group (`messenger.com/t/<id>`) in that browser, signed in as the bot account, and leave
the tab open. A small **LaneHub** label in the bottom-left corner shows what it is doing (only you
see it):

- **reading · N on screen** — messages are going to the hub.
- **draft in the box — press Enter to send** — a reply from the hub is in the message box. Read
  it, edit it if needed, and send it, or clear the box to drop it.
- **draft waiting — clear the message box** — you are typing yourself. The extension never types
  over that.

Only the tab with the lane's group is read. Other chats in the same browser are ignored.

## Limits and risks

- Messenger's web page changes without notice. Everything that depends on its markup is in
  `parser.js`. When reading stops, that file needs an update.
- Running a second account as a bot is against Facebook's terms. The account can be restricted
  or blocked. Tell the group members that this account is a bot and that its messages pass
  through LaneHub.
- Text and voice only. Photos and files are not read yet, and the hub cannot send files to a
  Messenger lane.
