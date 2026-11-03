#!/bin/zsh
# Build the downloadable Nimbus: a self-contained Nimbus.app (Python, Qt and every dependency inside,
# Apple Silicon) in a drag-to-Applications disk image, dist/Nimbus-<version>-arm64.dmg.
# Unlike scripts/build_app.sh, nothing on the user's Mac is needed to run it.
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
VERSION=$("$PY" -c "import nimbus; print(nimbus.__version__)")
"$PY" -m PyInstaller --version >/dev/null 2>&1 || "$PY" -m pip install -q "pyinstaller>=6.10"
mkdir -p dist

# Icon from the Nimbus mark.
ICONSET="$ROOT/dist/Nimbus.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s nimbus/assets/nimbus_mark.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) nimbus/assets/nimbus_mark.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o dist/Nimbus.icns
rm -rf "$ICONSET"

"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath dist/release --workpath build/pyinstaller packaging/Nimbus.spec
APP="$ROOT/dist/release/Nimbus.app"
# One ad-hoc signature over the whole bundle, so macOS keeps the permissions it grants to Nimbus.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

# Boot the bundled app invisibly (offscreen Qt, no event taps, a throwaway home) and require a clean start.
CHECK=$(mktemp -d)
QT_QPA_PLATFORM=offscreen NIMBUS_HOME="$CHECK" NIMBUS_NO_GLOBAL_INPUT=1 MODEL_API_KEY=boot-check \
  "$APP/Contents/MacOS/Nimbus" > "$CHECK/out.log" 2>&1 &
pid=$!
for _ in {1..120}; do
  grep -q "Nimbus started" "$CHECK/logs/nimbus.log" 2>/dev/null && break
  sleep 0.5
done
sleep 2  # let anything scheduled right after start run too
kill $pid 2>/dev/null; wait $pid 2>/dev/null || true
if ! grep -q "Nimbus started" "$CHECK/logs/nimbus.log" 2>/dev/null || grep -qE "ERROR|CRITICAL|Traceback" "$CHECK/out.log"; then
  echo "the bundled app did not boot cleanly:"; tail -40 "$CHECK/out.log"; exit 1
fi
rm -rf "$CHECK"
echo "boot check passed"

STAGE="$ROOT/dist/release/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="$ROOT/dist/Nimbus-$VERSION-arm64.dmg"
rm -f "$DMG"
hdiutil create -quiet -volname "Nimbus" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
shasum -a 256 "$DMG" | tee "$DMG.sha256"
echo "built $DMG ($(du -h "$DMG" | cut -f1))"
