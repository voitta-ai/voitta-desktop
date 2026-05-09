#!/usr/bin/env bash
# Build, optionally sign, optionally notarize Voitta Desktop.app (arm64).
#
# Usage:
#   scripts/build_app.sh                  # unsigned build
#   DEVELOPER_ID="Developer ID Application: NAME (TEAMID)" scripts/build_app.sh
#   DEVELOPER_ID=... NOTARY_PROFILE=voitta-notary scripts/build_app.sh
#
# Notary credentials — pick one of:
#   1. NOTARY_PROFILE  — keychain profile created via:
#        xcrun notarytool store-credentials voitta-notary \
#            --apple-id you@example.com --team-id ABC123 --password app-specific-pw
#   2. APPLE_ID + APP_PASSWORD + TEAM_ID  — passed inline (avoid in CI logs)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
APP="dist/Voitta Desktop.app"
ZIP="dist/VoittaDesktop.zip"

echo "==> Cleaning build/ dist/"
rm -rf build dist

echo "==> Ensuring icon (images/AppIcon.icns)"
[ -f images/AppIcon.icns ] || "$ROOT/scripts/make_icns.sh"

echo "==> py2app build (arm64)"
ARCHFLAGS="-arch arm64" "$PY" setup.py py2app

if [ ! -d "$APP" ]; then
    echo "ERROR: $APP not produced." >&2
    exit 1
fi

SIZE=$(du -sh "$APP" | cut -f1)
echo "==> Built: $APP ($SIZE)"

# ── Codesign ────────────────────────────────────────────────────────────
if [ -n "${DEVELOPER_ID:-}" ]; then
    echo "==> Codesigning with: $DEVELOPER_ID"
    # --deep for nested frameworks; --options runtime for hardened runtime (notary req)
    codesign --deep --force --timestamp --options runtime \
        --entitlements scripts/entitlements.plist \
        --sign "$DEVELOPER_ID" \
        "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
    echo "==> Signed."
else
    echo "==> DEVELOPER_ID not set — skipping codesign."
    echo "    Set to e.g. 'Developer ID Application: Your Name (TEAMID)' to enable."
fi

# ── Notarize ────────────────────────────────────────────────────────────
notarize() {
    echo "==> Zipping for notarization"
    ditto -c -k --keepParent "$APP" "$ZIP"

    if [ -n "${NOTARY_PROFILE:-}" ]; then
        echo "==> notarytool submit (profile: $NOTARY_PROFILE)"
        xcrun notarytool submit "$ZIP" \
            --keychain-profile "$NOTARY_PROFILE" \
            --wait
    else
        echo "==> notarytool submit (apple-id: $APPLE_ID, team: $TEAM_ID)"
        xcrun notarytool submit "$ZIP" \
            --apple-id "$APPLE_ID" \
            --password "$APP_PASSWORD" \
            --team-id "$TEAM_ID" \
            --wait
    fi

    echo "==> Stapling"
    xcrun stapler staple "$APP"
    xcrun stapler validate "$APP"
}

if [ -z "${DEVELOPER_ID:-}" ]; then
    echo "==> Notarization skipped (build is unsigned)."
elif [ -n "${NOTARY_PROFILE:-}" ] || { [ -n "${APPLE_ID:-}" ] && [ -n "${APP_PASSWORD:-}" ] && [ -n "${TEAM_ID:-}" ]; }; then
    notarize
else
    echo "==> Notarization skipped — set NOTARY_PROFILE or APPLE_ID/APP_PASSWORD/TEAM_ID."
fi

echo
echo "Done: $APP ($SIZE)"
