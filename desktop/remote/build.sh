#!/bin/sh
# Jarvis Remote (ADR 0406): build and sign the app next to this script.
#   sh build.sh           build "Jarvis Remote.app"
#   sh build.sh install   also copy it to ~/Applications and (re)start it as a LaunchAgent
# The first run asks, at the Mac, for Screen Recording and Accessibility (System Settings › Privacy & Security).
set -e
cd "$(dirname "$0")"
swift build -c release

app="Jarvis Remote.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp .build/release/JarvisRemote "$app/Contents/MacOS/"
cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.yilunshi.jarvis.remote</string>
  <key>CFBundleName</key><string>Jarvis Remote</string>
  <key>CFBundleExecutable</key><string>JarvisRemote</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
# a stable signature keeps the privacy grants across rebuilds
codesign --force --options runtime --sign "Developer ID Application: yilun shi (3MEBVQ3N3U)" "$app"

[ "$1" = install ] || exit 0
label=com.yilunshi.jarvis.remote
agent="$HOME/Library/LaunchAgents/$label.plist"
mkdir -p "$HOME/Applications"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
# bootout returns before the service is gone, and bootstrapping over it fails
while launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; do sleep 0.2; done
rm -rf "$HOME/Applications/$app"
cp -R "$app" "$HOME/Applications/"
cat > "$agent" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$HOME/Applications/$app/Contents/MacOS/JarvisRemote</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/jarvis-remote.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/jarvis-remote.log</string>
</dict></plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$agent"
echo "running; pair the iPad with: \"$HOME/Applications/$app/Contents/MacOS/JarvisRemote\" pair"
