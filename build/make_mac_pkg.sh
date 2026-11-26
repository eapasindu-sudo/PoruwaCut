#!/bin/bash
# Build dist/AutoWed_<ver>_mac.pkg from dist/PoruwaCutEngine.app + dist/PoruwaCut.ccx (+ build/ffmpeg/ffmpeg, ffprobe)
set -euo pipefail
cd "$(dirname "$0")/.."
VER=$(tr -d '[:space:]' < VERSION)
ROOT=build/pkgroot
rm -rf "$ROOT" build/pkgscripts
mkdir -p "$ROOT/Applications" "$ROOT/Library/Application Support/AutoWed" build/pkgscripts
APP="$ROOT/Applications/AutoWed Engine.app"
cp -R dist/PoruwaCutEngine.app "$APP"
mkdir -p "$APP/Contents/MacOS/ffmpeg"
cp build/ffmpeg/ffmpeg build/ffmpeg/ffprobe "$APP/Contents/MacOS/ffmpeg/"
[ -f build/ffmpeg/LICENSE.txt ] && cp build/ffmpeg/LICENSE.txt "$APP/Contents/MacOS/ffmpeg/" || true
chmod +x "$APP/Contents/MacOS/ffmpeg/"*
# background app: no Dock icon
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$APP/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" "$APP/Contents/Info.plist" 2>/dev/null || true
# Apple Silicon needs every binary signed: ad-hoc signature (no Apple Developer ID)
codesign --force --sign - "$APP/Contents/MacOS/ffmpeg/ffmpeg" "$APP/Contents/MacOS/ffmpeg/ffprobe"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --verbose=1 "$APP" || echo "codesign verify warning (ad-hoc)"
cp dist/PoruwaCut.ccx "$ROOT/Library/Application Support/AutoWed/PoruwaCut.ccx"
cp build/mac/uninstall.sh "$ROOT/Library/Application Support/AutoWed/uninstall.sh"
mkdir -p "$ROOT/Library/Application Support/AutoWed/resolve-plugin"
cp resolve-plugin/* "$ROOT/Library/Application Support/AutoWed/resolve-plugin/"
cp build/mac/postinstall build/pkgscripts/postinstall
chmod +x build/pkgscripts/postinstall
pkgbuild --root "$ROOT" --scripts build/pkgscripts --identifier lk.endora.autowed --version "$VER" \
  --install-location / "dist/AutoWed_${VER}_mac.pkg"
ls -la dist/AutoWed_${VER}_mac.pkg
