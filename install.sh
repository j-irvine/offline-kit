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

# The AI engine and the library reader both need macOS 14 (Sonoma) or newer.
MACOS="$(sw_vers -productVersion 2>/dev/null || echo 0)"
if [ "${MACOS%%.*}" -lt 14 ] 2>/dev/null; then
  fail "Offline Kit needs macOS 14 (Sonoma) or newer. This Mac has macOS $MACOS. Update it in System Settings > General > Software Update, then run this again."
fi

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
# Downloads are unpacked into a temporary folder and moved into place only when
# complete, so an interrupted install never leaves a half-written program behind.
if [ ! -x "$KIT/bin/uv" ]; then
  bold "Installing Python tools"
  rm -rf "$KIT/bin/.new" && mkdir -p "$KIT/bin/.new"
  curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$UV_ARCH.tar.gz" \
    | tar xz -C "$KIT/bin/.new" --strip-components 1 \
    || fail "couldn't download the Python tools (uv). Check your internet connection and try again."
  mv -f "$KIT/bin/.new/uv" "$KIT/bin/uv" && rm -rf "$KIT/bin/.new"
fi
python_works() { [ "$("$KIT/venv/bin/python" -c 'print(6 * 7)' 2>/dev/null)" = "42" ]; }
if ! python_works; then
  rm -rf "$KIT/venv"
  if ! "$KIT/bin/uv" venv --quiet --python "$PY_VERSION" --managed-python "$KIT/venv" || ! python_works; then
    # The private copy of Python may be damaged; fetch it again.
    rm -rf "$KIT/venv" "$KIT/python"
    "$KIT/bin/uv" venv --quiet --python "$PY_VERSION" --managed-python "$KIT/venv" \
      || fail "couldn't install Python. Check your internet connection and try again."
  fi
fi
"$KIT/bin/uv" pip install --quiet --python "$KIT/venv/bin/python" "libzim==$LIBZIM_VERSION" "pypdf==$PYPDF_VERSION" \
  || fail "couldn't install the library reader. Check your internet connection and try again."
"$KIT/venv/bin/python" -c "import libzim.reader, libzim.search, pypdf" \
  || fail "the library reader didn't install correctly. Delete the folder $KIT/venv and run this again."

# 3. Ollama, which runs the AI model. We keep a private copy so it doesn't
#    interfere with any Ollama you already have.
#    It is refreshed when a newer version is out, because newer AI models need it.
OLLAMA_LATEST="$(curl -fsSLI -o /dev/null -w '%{url_effective}' --max-time 20 \
  "https://github.com/ollama/ollama/releases/latest" 2>/dev/null | sed 's|.*/||' || true)"
case "$OLLAMA_LATEST" in v[0-9]*) ;; *) OLLAMA_LATEST="" ;; esac
OLLAMA_HAVE="$(cat "$KIT/ollama/.version" 2>/dev/null || true)"
if [ ! -x "$KIT/ollama/ollama" ] || { [ -n "$OLLAMA_LATEST" ] && [ "$OLLAMA_LATEST" != "$OLLAMA_HAVE" ]; }; then
  bold "Installing Ollama (the AI engine, ~190 MB)"
  rm -rf "$KIT/ollama.new" && mkdir -p "$KIT/ollama.new"
  if curl -fL --progress-bar --retry 3 "https://github.com/ollama/ollama/releases/latest/download/ollama-darwin.tgz" \
      | tar xz -C "$KIT/ollama.new" && [ -x "$KIT/ollama.new/ollama" ]; then
    printf '%s\n' "$OLLAMA_LATEST" > "$KIT/ollama.new/.version"
    rm -rf "$KIT/ollama" && mv "$KIT/ollama.new" "$KIT/ollama"
  else
    rm -rf "$KIT/ollama.new"
    [ -x "$KIT/ollama/ollama" ] || fail "couldn't download Ollama. Check your internet connection and try again."
    echo "Couldn't download the newer Ollama; keeping the one already installed."
  fi
fi

# 4. Everything else (choosing a size, downloading the library and the model,
#    creating the Desktop shortcut) is done by setup.py. caffeinate keeps the
#    Mac awake while it runs.
rm -rf "$KIT/.cache"
exec caffeinate -i "$KIT/venv/bin/python" "$KIT/kit/app/setup.py" "$@"
