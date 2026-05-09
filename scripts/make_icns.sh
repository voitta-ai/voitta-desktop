#!/usr/bin/env bash
# Build images/AppIcon.icns from images/icon.png using sips + iconutil (no extra deps).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SRC=images/icon.png
DEST=images/AppIcon.icns
ICONSET=build/AppIcon.iconset

[ -f "$SRC" ] || { echo "ERROR: $SRC missing." >&2; exit 1; }

mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$SRC" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    doubled=$((size * 2))
    sips -z "$doubled" "$doubled" "$SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done

iconutil -c icns "$ICONSET" -o "$DEST"
echo "Built $DEST"
