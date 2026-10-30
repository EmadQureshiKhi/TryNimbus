#!/bin/zsh
# Build dist/Nimbus.app: a tiny native launcher that execs the repo's venv Python.
# Launching through a real app bundle makes macOS attribute Screen Recording, Accessibility and
# Microphone permissions to "Nimbus" (and shows our usage strings) instead of to Terminal.
set -euo pipefail
ROOT="${0:A:h:h}"
APP="$ROOT/dist/Nimbus.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Nimbus</string>
  <key>CFBundleDisplayName</key><string>Nimbus</string>
  <key>CFBundleIdentifier</key><string>ai.nimbus.mac</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleExecutable</key><string>Nimbus</string>
  <key>CFBundleIconFile</key><string>Nimbus</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Nimbus listens while you hold the talk key so you can ask about your screen.</string>
  <key>NSScreenCaptureUsageDescription</key><string>Nimbus looks at your screen when you ask a question, so it can point at what it is talking about.</string>
  <key>NSAppleEventsUsageDescription</key><string>Nimbus can open apps for you when you ask it to.</string>
</dict></plist>
PLIST

cat > "$ROOT/dist/launcher.c" <<C
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
int main(void) {
  setenv("PYTHONPATH", "$ROOT", 1);
  chdir("$ROOT");
  char *args[] = {"$ROOT/.venv/bin/python", "-m", "nimbus", NULL};
  execv(args[0], args);
  perror("Nimbus: could not start Python");
  return 1;
}
C
clang -O2 -o "$APP/Contents/MacOS/Nimbus" "$ROOT/dist/launcher.c"
rm "$ROOT/dist/launcher.c"

# Icon from the Nimbus mark.
ICONSET="$ROOT/dist/Nimbus.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$ROOT/nimbus/assets/nimbus_mark.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$ROOT/nimbus/assets/nimbus_mark.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Nimbus.icns"
rm -rf "$ICONSET"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "built $APP"
