#!/usr/bin/env bash
set -euo pipefail

APP_NAME="VideoCaptioner"
DEFAULT_VERSION="macos-enhanced-v0.1.3"
VERSION="${VIDEO_CAPTIONER_VERSION:-$DEFAULT_VERSION}"
if [[ ! "$VERSION" =~ ^macos-enhanced-v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    echo "Invalid release version: $VERSION" >&2
    echo "Expected format: macos-enhanced-vX.Y.Z" >&2
    exit 1
fi
APP_VERSION="${BASH_REMATCH[1]}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
PYINSTALLER_BIN="$PROJECT_ROOT/.venv/bin/pyinstaller"
DIST_DIR="$PROJECT_ROOT/dist"
BUILD_DIR="$PROJECT_ROOT/build"
ICON_FILE="$DIST_DIR/$APP_NAME.app/Contents/Resources/AppIcon.icns"
PYINSTALLER_DIST="$DIST_DIR/pyinstaller"
PYINSTALLER_WORK="$BUILD_DIR/pyinstaller"
RELEASE_DIR="$DIST_DIR/release"
STAGING_DIR="$DIST_DIR/dmg-staging"
APP_PATH="$PYINSTALLER_DIST/$APP_NAME.app"
DMG_PATH="$RELEASE_DIR/$APP_NAME-$VERSION.dmg"
CHECKSUM_PATH="$DMG_PATH.sha256"
BUNDLE_ID="com.meguruno1.videocaptioner.macosenhanced"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Missing virtual environment Python: $PYTHON_BIN" >&2
    exit 1
fi
if [[ ! -x "$PYINSTALLER_BIN" ]]; then
    echo "Missing PyInstaller. Install it in .venv before building a release." >&2
    exit 1
fi
if ! command -v hdiutil >/dev/null 2>&1; then
    echo "Missing required macOS disk image tool: hdiutil" >&2
    exit 1
fi

rm -rf "$PYINSTALLER_DIST" "$PYINSTALLER_WORK" "$STAGING_DIR"
rm -f "$DMG_PATH" "$CHECKSUM_PATH"
mkdir -p "$RELEASE_DIR"

# Reuse the local launcher script only for its icon generation path.
bash "$PROJECT_ROOT/scripts/build_macos_app.sh" >/dev/null

"$PYINSTALLER_BIN" \
    --noconfirm \
    --clean \
    --windowed \
    --name "$APP_NAME" \
    --distpath "$PYINSTALLER_DIST" \
    --workpath "$PYINSTALLER_WORK" \
    --specpath "$PYINSTALLER_WORK" \
    --icon "$ICON_FILE" \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --add-data "$PROJECT_ROOT/resource/assets/logo.png:resource/assets" \
    --add-data "$PROJECT_ROOT/app/core/utils/acceleration.py:app/core/utils" \
    --add-binary "$(command -v ffmpeg):resource/bin/macos-arm64" \
    --add-binary "$(command -v ffprobe):resource/bin/macos-arm64" \
    --collect-all whisperx \
    --collect-all mlx_whisper \
    --collect-all mlx \
    --copy-metadata torchcodec \
    --hidden-import whisperx \
    --hidden-import mlx_whisper \
    --hidden-import torch \
    --hidden-import torchaudio \
    --hidden-import torchvision \
    --hidden-import pyannote.audio \
    --hidden-import faster_whisper \
    --hidden-import ctranslate2 \
    "$PROJECT_ROOT/main.py"

"$PYTHON_BIN" - "$APP_PATH/Contents/Info.plist" "$APP_VERSION" "$BUNDLE_ID" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
version = sys.argv[2]
bundle_id = sys.argv[3]

with plist_path.open("rb") as file:
    plist = plistlib.load(file)

plist["CFBundleIdentifier"] = bundle_id
plist["CFBundleShortVersionString"] = version
plist["CFBundleVersion"] = version
plist["CFBundleName"] = "VideoCaptioner"
plist["CFBundleDisplayName"] = "VideoCaptioner"

with plist_path.open("wb") as file:
    plistlib.dump(plist, file)
PY

codesign --force --deep --sign - "$APP_PATH" >/dev/null
codesign --verify --deep --strict "$APP_PATH"

mkdir -p "$STAGING_DIR"
cp -R "$APP_PATH" "$STAGING_DIR/$APP_NAME.app"
ln -s /Applications "$STAGING_DIR/Applications"
cat > "$STAGING_DIR/README.txt" <<EOF
VideoCaptioner macOS Enhanced

Drag VideoCaptioner.app into Applications, then open it from Applications.

This package bundles Python, Python dependencies, FFmpeg and ffprobe.
ASR models are downloaded on first use. Network requirements of external
services such as YouTube still apply.

WhisperX and MLX Whisper models download on first use unless you already have
compatible models under:

~/Library/Application Support/VideoCaptioner/models

This app is ad-hoc signed and not Apple-notarized. If macOS blocks first launch,
right-click VideoCaptioner.app and choose Open.
EOF

hdiutil create \
    -volname "VideoCaptioner macOS Enhanced" \
    -srcfolder "$STAGING_DIR" \
    -ov \
    -format UDZO \
    "$DMG_PATH" >/dev/null
rm -rf "$STAGING_DIR"
hdiutil verify "$DMG_PATH" >/dev/null

(
    cd "$RELEASE_DIR"
    shasum -a 256 "$(basename "$DMG_PATH")" > "$(basename "$CHECKSUM_PATH")"
)

echo "Built $DMG_PATH"
echo "Wrote $CHECKSUM_PATH"
