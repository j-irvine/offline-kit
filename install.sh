#!/bin/bash
# Offline Kit installer for macOS.
#
# Run it from a downloaded copy of the repository (see README.md):
#   bash ~/Downloads/offline-kit-main/install.sh
# If the repository is public, it can also be piped straight from GitHub:
#   curl -fsSL https://raw.githubusercontent.com/j-irvine/offline-kit/main/install.sh | bash
#
# It is safe to run again: finished downloads are kept, partial ones resume.
# Everything goes in ~/OfflineKit. Deleting that folder (and the Desktop
# shortcut) removes it completely.
set -euo pipefail

REPO="${OFFLINE_KIT_REPO:-j-irvine/offline-kit}"
BRANCH="${OFFLINE_KIT_BRANCH:-main}"
KIT="${OFFLINE_KIT_HOME:-$HOME/OfflineKit}"
PY_VERSION="3.12"
LIBZIM_VERSION="4.1.0"
PYPDF_VERSION="7.1.0"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mSomething went wrong: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this installer only works on a Mac."
case "$(uname -m)" in
  arm64) UV_ARCH="aarch64-apple-darwin" ;;
  x86_64) UV_ARCH="x86_64-apple-darwin" ;;
  *) fail "unsupported processor: $(uname -m)" ;;
esac

bold "Setting up Offline Kit in $KIT"
mkdir -p "$KIT/bin" "$KIT/library" "$KIT/models"

# 1. Get the kit's code. Use the folder this script is in when run from a
#    checkout; otherwise download the latest version from GitHub.
SRC_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/app/server.py" ]; then
  SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SRC_DIR" ] && [ "$SRC_DIR" != "$KIT/kit" ]; then
  bold "Copying the kit from $SRC_DIR"
  rm -rf "$KIT/kit" && mkdir -p "$KIT/kit"
  (cd "$SRC_DIR" && tar cf - --exclude .git .) | (cd "$KIT/kit" && tar xf -)
elif [ -z "$SRC_DIR" ]; then
  bold "Downloading the kit"
  TMP="$(mktemp -d)"
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar xz -C "$TMP" \
    || fail "couldn't download the kit. Check your internet connection and try again."
  rm -rf "$KIT/kit" && mv "$TMP"/*/ "$KIT/kit" && rm -rf "$TMP"
fi

# 2. Python (a private copy, managed by uv; nothing touches the system Python).
export UV_PYTHON_INSTALL_DIR="$KIT/python" UV_CACHE_DIR="$KIT/.cache/uv" UV_NO_CONFIG=1
if [ ! -x "$KIT/bin/uv" ]; then
  bold "Installing Python tools"
  curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$UV_ARCH.tar.gz" \
    | tar xz -C "$KIT/bin" --strip-components 1 || fail "couldn't download uv."
fi
if [ ! -x "$KIT/venv/bin/python" ]; then
  "$KIT/bin/uv" venv --quiet --python "$PY_VERSION" --managed-python "$KIT/venv"
fi
"$KIT/bin/uv" pip install --quiet --python "$KIT/venv/bin/python" "libzim==$LIBZIM_VERSION" "pypdf==$PYPDF_VERSION"

# 3. Ollama, which runs the AI model. We keep a private copy so it doesn't
#    interfere with any Ollama you already have.
if [ ! -x "$KIT/ollama/ollama" ]; then
  bold "Installing Ollama (the AI engine, ~190 MB)"
  mkdir -p "$KIT/ollama"
  curl -fL --progress-bar "https://github.com/ollama/ollama/releases/latest/download/ollama-darwin.tgz" \
    | tar xz -C "$KIT/ollama" || fail "couldn't download Ollama."
fi

# 4. Everything else (choosing a size, downloading the library and the model,
#    creating the Desktop shortcut) is done by setup.py. caffeinate keeps the
#    Mac awake while it runs.
rm -rf "$KIT/.cache"
exec caffeinate -i "$KIT/venv/bin/python" "$KIT/kit/app/setup.py" "$@"
