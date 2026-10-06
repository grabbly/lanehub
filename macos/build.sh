#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

VERSION=$(python3 -c "import sys; sys.path.insert(0, '${ROOT_DIR}'); from app.config import VERSION; print(VERSION)" 2>/dev/null || echo "0.8.0")

BUILD_DIR="${SCRIPT_DIR}/build"
APP_NAME="LaneHub Autopilot"
APP_BUNDLE="${BUILD_DIR}/${APP_NAME}.app"
ZIP_NAME="${BUILD_DIR}/LaneHubAutopilot.zip"

echo "Building ${APP_NAME} ${VERSION}..."

rm -rf "${APP_BUNDLE}" "${ZIP_NAME}"
mkdir -p "${APP_BUNDLE}/Contents/MacOS"
mkdir -p "${APP_BUNDLE}/Contents/Resources"

# Update/copy Info.plist with current version
sed -e "s/<string>0.8.0<\/string>/<string>${VERSION}<\/string>/g" "${SCRIPT_DIR}/Info.plist" > "${APP_BUNDLE}/Contents/Info.plist"
plutil -lint "${APP_BUNDLE}/Contents/Info.plist" > /dev/null

echo "Compiling Swift app..."
swiftc -O -parse-as-library "${SCRIPT_DIR}/LaneHubAutopilotApp.swift" -o "${APP_BUNDLE}/Contents/MacOS/${APP_NAME}"

echo "Codesigning app bundle..."
codesign --force --deep -s - "${APP_BUNDLE}"

echo "Creating distribution zip..."
ditto -c -k --keepParent "${APP_BUNDLE}" "${ZIP_NAME}"

echo "Built successfully:"
echo "  App: ${APP_BUNDLE}"
echo "  Zip: ${ZIP_NAME}"
