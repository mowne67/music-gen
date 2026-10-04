#!/bin/bash
# Run from a checkout, or download and execute to install into ./music-gen.
set -euo pipefail

REPO_URL="https://github.com/mowne67/music-gen.git"
ACE_COMMIT="ca1e85fe9430179831e6bc6be790c332190a3866"
ART_MODEL="dhairyashil/FLUX.1-schnell-mflux-v0.6.2-4bit"
STEP="checking your Mac"
trap 'printf "\nSetup stopped while %s. Fix the error above, then rerun setup.\n" "$STEP" >&2' ERR

fail() {
    printf '\n%s\n' "$1" >&2
    exit 1
}

step() {
    STEP="$1"
    printf '\n==> %s\n' "$STEP"
}

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    fail "This installer needs macOS on Apple Silicon. If using Rosetta, open a native Terminal and rerun."
fi

step "installing system tools"
if ! command -v brew >/dev/null 2>&1; then
    if [[ -x /opt/homebrew/bin/brew ]]; then
        export PATH="/opt/homebrew/bin:$PATH"
    else
        BREW_INSTALLER="$(mktemp -t music-gen-homebrew)"
        curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$BREW_INSTALLER"
        /bin/bash "$BREW_INSTALLER"
        rm -f "$BREW_INSTALLER"
        export PATH="/opt/homebrew/bin:$PATH"
    fi
fi
TOOLS=()
if ! git --version >/dev/null 2>&1; then TOOLS+=(git); fi
if ! command -v uv >/dev/null 2>&1; then TOOLS+=(uv); fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    TOOLS+=(ffmpeg)
fi
if [[ ${#TOOLS[@]} -gt 0 ]]; then brew install "${TOOLS[@]}"; fi

step "preparing music-gen"
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [[ -n "$SCRIPT_PATH" && -f "$SCRIPT_PATH" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd -P)"
else
    SCRIPT_DIR=""
fi
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/make_video.py" && -f "$SCRIPT_DIR/requirements-art.txt" ]]; then
    ROOT="$SCRIPT_DIR"
elif [[ -f "$PWD/make_video.py" && -f "$PWD/requirements-art.txt" && -f "$PWD/docs/acestep-macos-launcher.patch" ]]; then
    ROOT="$PWD"
else
    ROOT="${MUSIC_GEN_DIR:-$PWD/music-gen}"
    if [[ ! -e "$ROOT" ]]; then
        git clone "$REPO_URL" "$ROOT"
    elif [[ ! -f "$ROOT/make_video.py" || ! -f "$ROOT/requirements-art.txt" || ! -f "$ROOT/docs/acestep-macos-launcher.patch" ]]; then
        fail "$ROOT already exists and is not a music-gen checkout. Run setup from another folder or set MUSIC_GEN_DIR."
    fi
fi
cd "$ROOT"
ROOT="$(pwd -P)"

step "installing Python and art dependencies"
if [[ ! -x .venv-flux/bin/python ]]; then
    uv venv .venv-flux --python 3.12
fi
uv pip install --python .venv-flux/bin/python -r requirements-art.txt

step "configuring ACE-Step"
ACE="$ROOT/ACE-Step-1.5"
if [[ ! -e "$ACE" ]]; then
    git clone --filter=blob:none --no-checkout https://github.com/ace-step/ACE-Step-1.5.git "$ACE"
    git -C "$ACE" checkout "$ACE_COMMIT"
elif [[ "$(git -C "$ACE" rev-parse --show-toplevel 2>/dev/null || true)" != "$ACE" ]]; then
    fail "$ACE already exists and is not an ACE-Step Git checkout. Move it aside before rerunning setup."
fi
if [[ "$(git -C "$ACE" rev-parse HEAD)" != "$ACE_COMMIT" ]]; then
    if [[ -n "$(git -C "$ACE" status --porcelain)" ]]; then
        fail "Your ACE-Step checkout has local changes at another revision. Save them before switching to $ACE_COMMIT."
    fi
    if ! git -C "$ACE" cat-file -e "$ACE_COMMIT^{commit}" 2>/dev/null; then
        git -C "$ACE" fetch origin "$ACE_COMMIT"
    fi
    git -C "$ACE" checkout "$ACE_COMMIT"
fi
PATCH="$ROOT/docs/acestep-macos-launcher.patch"
if git -C "$ACE" apply --check "$PATCH" 2>/dev/null; then
    git -C "$ACE" apply "$PATCH"
elif ! git -C "$ACE" apply --reverse --check "$PATCH" 2>/dev/null; then
    fail "The ACE-Step launcher has conflicting changes. Save them and restore the pinned launcher before rerunning setup."
fi
uv sync --directory "$ACE" --python 3.12 --locked

step "downloading artwork weights (about 9.6 GB; completed files are reused)"
.venv-flux/bin/hf download "$ART_MODEL" --local-dir models/flux-schnell-4bit

printf '\nReady! ACE-Step will download its music models on the first generation.\n'
printf 'To make your first video:\n\n'
printf '  cd %q\n' "$ROOT"
printf '  .venv-flux/bin/python make_video.py first_track "Calm rainy-night piano, soft drums, mellow bass. No vocals." 1 30\n\n'
