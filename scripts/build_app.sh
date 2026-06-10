#!/usr/bin/env bash
# Build the Voitta Desktop .app via briefcase.
#
# Lives at scripts/ but cd's to repo root because briefcase reads
# pyproject.toml from CWD and writes build/ + dist/ next to it.
#
#   ./scripts/build_app.sh                 # standalone build (~2-4 min)
#   ./scripts/build_app.sh --clean         # nuke build/ dist/ wheels/ first
#   ./scripts/build_app.sh --bump          # bump patch version before building
#   ./scripts/build_app.sh --package       # also produce a .dmg in dist/ (ad-hoc)
#
# Code-sign + notarise (one-command frictionless distribution):
#
#   ./scripts/build_app.sh --package \
#       --sign "Developer ID Application: roman semeine (KU3WTX9RXB)" \
#       --notarize
#
# Output:
#   build/voitta_desktop/macos/app/Voitta Desktop.app   # the bundle
#   dist/Voitta Desktop-0.1.0.dmg                       # only with --package
#
# Distribution friction by mode:
#
#   --package                 — ad-hoc signed. Recipient sees Gatekeeper
#                               warning, must right-click → Open the
#                               first time. Free.
#   --package --sign …        — Developer ID signed. Removes "unidentified
#                               developer" wording, but on macOS 10.15+
#                               Gatekeeper still wants notarisation.
#   --package --sign … --notarize
#                             — fully Gatekeeper-clean. Recipient
#                               double-clicks, no warning, no friction.
#
# Notarisation prerequisite (one-time, already done on this machine for
# voitta-bookmarklet — same keychain profile is reused):
#
#   xcrun notarytool store-credentials voitta-notary \
#     --apple-id you@example.com --team-id KU3WTX9RXB
#   # (paste the app-specific password from
#   #  https://appleid.apple.com → Sign-In and Security)
#
#   The keychain profile name "voitta-notary" is what --notarize looks
#   up; override via $VOITTA_NOTARY_PROFILE env var.

set -euo pipefail

# Resolve repo root from this script's location.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"

CLEAN=0
BUMP=0
PACKAGE=0
SIGN_IDENTITY=""
NOTARIZE=0
NOTARY_PROFILE="${VOITTA_NOTARY_PROFILE:-voitta-notary}"

# Manual arg loop because --sign takes a value containing spaces and
# parens, e.g. "Developer ID Application: roman semeine (KU3WTX9RXB)".
while [ $# -gt 0 ]; do
  case "$1" in
    --clean)    CLEAN=1; shift ;;
    --bump)     BUMP=1;  shift ;;
    --package)  PACKAGE=1; shift ;;
    --sign)
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "[build_app] --sign needs an identity string" >&2
        exit 2
      fi
      SIGN_IDENTITY="$2"; shift 2 ;;
    --notarize) NOTARIZE=1; shift ;;
    -h|--help)
      sed -n '2,55p' "$0"; exit 0 ;;
    *)
      echo "[build_app] unknown arg: $1" >&2
      exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Version bump + stamp
# ---------------------------------------------------------------------------
if [ "$BUMP" -eq 1 ]; then
  CURRENT_VER=$("$VENV/bin/python" - <<'PYEOF'
import tomllib
with open("pyproject.toml", "rb") as f:
    d = tomllib.load(f)
print(d["tool"]["briefcase"]["version"])
PYEOF
)
  NEW_VER=$("$VENV/bin/python" - "$CURRENT_VER" <<'PYEOF'
import sys
parts = sys.argv[1].split(".")
parts[-1] = str(int(parts[-1]) + 1)
print(".".join(parts))
PYEOF
)
  sed -i '' "s/^version = \"$CURRENT_VER\"/version = \"$NEW_VER\"/" pyproject.toml
  echo "[build_app] version bump: $CURRENT_VER → $NEW_VER"
fi

VERSION=$("$VENV/bin/python" - <<'PYEOF'
import tomllib
with open("pyproject.toml", "rb") as f:
    d = tomllib.load(f)
print(d["tool"]["briefcase"]["version"])
PYEOF
)
echo "[build_app] building version $VERSION"

# Stamp version into the package so the frozen .app can read it at runtime.
echo "__version__ = \"$VERSION\"" > "$ROOT/src/voitta_desktop/_version.py"

if [ "$NOTARIZE" -eq 1 ] && [ -z "$SIGN_IDENTITY" ]; then
  echo "[build_app] --notarize requires --sign \"<Developer ID identity>\" — Apple won't notarise an ad-hoc bundle." >&2
  exit 2
fi
if [ -n "$SIGN_IDENTITY" ] && [ "$PACKAGE" -eq 0 ]; then
  echo "[build_app] --sign without --package has no effect (briefcase only signs at package time). Add --package." >&2
  exit 2
fi

if [ ! -d "$VENV" ]; then
  echo "[build_app] no .venv found — run:  python -m venv .venv && .venv/bin/pip install -r requirements.txt briefcase" >&2
  exit 1
fi

# Make sure briefcase is installed in the venv. We don't add briefcase to
# requirements.txt because terminal dev doesn't need it — only this script.
echo "[build_app] ensuring briefcase is installed..."
"$VENV/bin/pip" install -q briefcase

if [ "$CLEAN" -eq 1 ]; then
  echo "[build_app] cleaning build/, dist/, wheels/..."
  rm -rf "$ROOT/build" "$ROOT/dist" "$ROOT/wheels"
fi

# Generate the .app icon if it isn't already there.
if [ ! -f "$ROOT/images/AppIcon.icns" ]; then
  echo "[build_app] generating images/AppIcon.icns..."
  "$ROOT/scripts/make_icns.sh"
fi

# rumps is sdist-only on PyPI; briefcase passes --only-binary :all: to pip
# so we pre-build a wheel locally and reference it via relative path in
# pyproject.toml's `requires` list.
mkdir -p "$ROOT/wheels"
if ! ls "$ROOT/wheels"/rumps-*.whl >/dev/null 2>&1; then
  echo "[build_app] pre-building rumps wheel..."
  "$VENV/bin/pip" wheel --no-deps --quiet -w "$ROOT/wheels" rumps
fi

# briefcase create — downloads CPython support package, installs deps into
# the bundle's standalone Python. Idempotent if no spec changes.
APP_DIR="$ROOT/build/voitta_desktop/macos/app/Voitta Desktop.app"
if [ ! -d "$APP_DIR" ] || [ "$CLEAN" -eq 1 ]; then
  echo "[build_app] briefcase create..."
  "$VENV/bin/briefcase" create macOS app --no-input
fi

# briefcase update — re-stages source + resources without re-installing
# wheels. Cheap; keeps the bundle in sync with local edits.
echo "[build_app] briefcase update (sync source + resources)..."
"$VENV/bin/briefcase" update macOS app --no-input

# briefcase build — strips, signs (ad-hoc by default).
echo "[build_app] briefcase build (ad-hoc sign)..."
"$VENV/bin/briefcase" build macOS app --no-input

if [ ! -d "$APP_DIR" ]; then
  echo "[build_app] briefcase reported success but $APP_DIR is missing." >&2
  exit 1
fi

if [ "$PACKAGE" -eq 1 ]; then
  # Remove previous DMGs so only the current build remains in dist/.
  if ls "$ROOT/dist/"*.dmg "$ROOT/dist/"*.dmg.zip 2>/dev/null | grep -q .; then
    echo "[build_app] cleaning old dist/ artefacts..."
    rm -f "$ROOT/dist/"*.dmg "$ROOT/dist/"*.dmg.zip
  fi

  if [ -z "$SIGN_IDENTITY" ]; then
    echo "[build_app] briefcase package (DMG, ad-hoc sign)..."
    "$VENV/bin/briefcase" package macOS app --adhoc-sign --no-input
  else
    echo "[build_app] briefcase package — signing as: $SIGN_IDENTITY"
    "$VENV/bin/briefcase" package macOS app \
      --identity "$SIGN_IDENTITY" \
      --no-input

    if [ "$NOTARIZE" -eq 1 ]; then
      DMG=$(ls -1 "$ROOT/dist/"*.dmg 2>/dev/null | head -1)
      if [ -z "$DMG" ]; then
        echo "[build_app] expected a .dmg under dist/ but none found." >&2
        exit 1
      fi
      echo "[build_app] notarising $DMG (profile: $NOTARY_PROFILE)..."
      if ! xcrun notarytool submit "$DMG" \
           --keychain-profile "$NOTARY_PROFILE" \
           --wait; then
        echo "[build_app] notarytool reported failure." >&2
        echo "[build_app]   (run: xcrun notarytool log <submission-id> --keychain-profile $NOTARY_PROFILE)" >&2
        exit 1
      fi
      echo "[build_app] stapling notarisation ticket..."
      xcrun stapler staple "$DMG"
      echo "[build_app] verifying with spctl..."
      spctl -a -vv --type install "$DMG" || true
    fi
  fi

  # Guard against silent empty DMGs: briefcase ignores a failed `ditto`
  # (e.g. com.apple.provenance EPERM when the build host lacks Full Disk
  # Access) and ships a notarised-but-empty image. Mount and confirm the
  # .app is actually inside.
  DMG=$(ls -1 "$ROOT/dist/"*.dmg 2>/dev/null | head -1)
  if [ -n "$DMG" ]; then
    MP=$(mktemp -d)
    if hdiutil attach "$DMG" -mountpoint "$MP" -nobrowse -quiet; then
      if ls -d "$MP/"*.app >/dev/null 2>&1; then
        echo "[build_app] verified: .app present inside $DMG"
        hdiutil detach "$MP" -quiet || true
      else
        echo "[build_app] ERROR: $DMG contains no .app — packaging dropped the bundle." >&2
        echo "[build_app]   Likely cause: build host lacks Full Disk Access (ditto EPERM on com.apple.provenance)." >&2
        hdiutil detach "$MP" -quiet || true
        rmdir "$MP" 2>/dev/null || true
        exit 1
      fi
    fi
    rmdir "$MP" 2>/dev/null || true
  fi
fi

echo
echo "[build_app] done."
echo "    $APP_DIR"
du -sh "$APP_DIR" 2>/dev/null | sed 's/^/    size  /'
if [ "$PACKAGE" -eq 1 ]; then
  ls -1 dist/*.dmg 2>/dev/null | sed 's/^/    dmg   /'
fi
echo
echo "    open \"$APP_DIR\""
echo "    .venv/bin/briefcase run macOS app   # alt: streams stdout/stderr"
