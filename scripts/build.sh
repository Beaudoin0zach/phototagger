#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$ROOT/.build"
mkdir -p "$ROOT/.build/module-cache"

# Prefer the complete Xcode toolchain when present. On macOS beta/update
# boundaries, CommandLineTools can temporarily contain a compiler and SDK from
# different patch releases. Keeping the module cache inside the project also
# makes builds work in sandboxed automation environments.
if [ -d /Applications/Xcode.app/Contents/Developer ]; then
  DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
  export DEVELOPER_DIR
fi
CLANG_MODULE_CACHE_PATH="$ROOT/.build/module-cache"
SWIFT_MODULECACHE_PATH="$ROOT/.build/module-cache"
export CLANG_MODULE_CACHE_PATH SWIFT_MODULECACHE_PATH

xcrun swiftc \
  "$ROOT/Sources/PhotoClassifier.swift" \
  -o "$ROOT/.build/phototagger-classify" \
  -framework Foundation \
  -framework Vision \
  -framework CoreGraphics \
  -framework ImageIO

echo "Built $ROOT/.build/phototagger-classify"
