#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export CLANG_MODULE_CACHE_PATH="$PWD/.build/clang-cache"
swift build -c release --disable-sandbox -debug-info-format none --cache-path "$PWD/.build/cache"
app="$PWD/dist/Caption Translator.app"
mkdir -p "$app/Contents/MacOS"
cp .build/release/CaptionTranslator "$app/Contents/MacOS/CaptionTranslator"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>CaptionTranslator</string>
  <key>CFBundleIdentifier</key><string>local.allen.caption-translator</string>
  <key>CFBundleName</key><string>字幕翻译</string>
  <key>CFBundleDisplayName</key><string>字幕翻译</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>26.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$app"
printf '%s\n' "$app"
