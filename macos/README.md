# LaneHub Autopilot (macOS)

Native macOS menu-bar helper for LaneHub bot auto-replies.

## Core Rule: No Autopilot → No Process

The bot watcher runs only while Autopilot is active and exits automatically when turned off. LaneHub Autopilot provides visible control and status right in your macOS menu bar.

## Requirements

- macOS 13.0 (Ventura) or newer.
- Swift command-line tools (installed with Xcode Command Line Tools: `xcode-select --install`).

## Build

To build the application and distribution zip from source:

```bash
./macos/build.sh
```

This creates:
- `macos/build/LaneHub Autopilot.app` (ad-hoc codesigned)
- `macos/build/LaneHubAutopilot.zip`

## Installation

1. Download `LaneHubAutopilot.zip` from the [latest release](https://github.com/grabbly/lanehub/releases/latest/download/LaneHubAutopilot.zip) or build it locally.
2. Unzip and drag `LaneHub Autopilot.app` to your `/Applications` folder.
3. Because the app is ad-hoc signed, on first launch:
   - Right-click (or Control-click) `LaneHub Autopilot.app` in `/Applications`.
   - Select **Open**.
   - Click **Open** in the macOS Gatekeeper confirmation prompt.

## Usage

1. **Add Project**:
   - Click the paperplane icon in the menu bar.
   - Choose **Add project…** and pick your repository/project folder containing `.lanehub.env`.
2. **Start Autopilot**:
   - Click **Start (8 h)** under your project.
   - The app enables autopilot on the hub and launches the watcher as a child process.
   - Autopilot output is logged to `.lanehub-autopilot.log` inside the project folder.
3. **Stop Autopilot**:
   - Click **Stop** under the project, or anyone in the chat can run `/autopilot @bot off`.
   - The watcher automatically terminates within seconds.
4. **URL Scheme**:
   - Start: `open "lanehub-autopilot://start?dir=/path/to/project&hours=8"`
   - Stop: `open "lanehub-autopilot://stop?dir=/path/to/project"`
   This is used by `./tg-autopilot.sh on [hours]` and `./tg-autopilot.sh off`.
5. **Quit**:
   - Choose **Quit LaneHub Autopilot** to cleanly stop all active watchers and exit.
