#!/usr/bin/env bash
# kit bootstrap for Linux and macOS:
#   curl -fsSL https://raw.githubusercontent.com/kevaldeepak/kit/main/bootstrap.sh | bash
#
# Installs uv (user-level, no admin), a private Python, downloads kit and
# hands over to `kit setup`. Safe to re-run: it updates kit in place.
#
# Env overrides: KIT_REPO (owner/name), KIT_REF (branch), KIT_SOURCE (local
# folder instead of GitHub), KIT_HOME, KIT_NONINTERACTIVE=1.
set -euo pipefail

main() {
  local repo="${KIT_REPO:-kevaldeepak/kit}"
  local ref="${KIT_REF:-main}"
  local kit_home="${KIT_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/kit}"
  local tmp
  tmp="$(mktemp -d)"
  trap "rm -rf '$tmp'" EXIT

  case "$(uname -s)" in
    Linux|Darwin) ;;
    *) echo "This is for Linux and macOS. On Windows use bootstrap.ps1." >&2; exit 1 ;;
  esac
  if [ "$(id -u)" = "0" ] && [ -z "${KIT_ALLOW_ROOT:-}" ]; then
    echo "Note: running as root, so kit installs for root. Set KIT_ALLOW_ROOT=1 to hide this." >&2
  fi
  command -v curl >/dev/null || { echo "curl is needed." >&2; exit 1; }
  command -v tar >/dev/null || { echo "tar is needed." >&2; exit 1; }

  echo "[1/4] Getting uv (Python manager, user-level)"
  local uv
  uv="$(command -v uv || true)"
  if [ -z "$uv" ] && [ -x "$HOME/.local/bin/uv" ]; then uv="$HOME/.local/bin/uv"; fi
  if [ -z "$uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
    uv="$HOME/.local/bin/uv"
  fi
  [ -x "$uv" ] || { echo "uv install failed." >&2; exit 1; }

  echo "[2/4] Setting up Python in $kit_home/venv"
  if [ ! -x "$kit_home/venv/bin/python" ]; then
    "$uv" venv --quiet --python 3.12 "$kit_home/venv"
  fi
  "$uv" pip install --quiet --python "$kit_home/venv/bin/python" pyyaml
  local py="$kit_home/venv/bin/python"

  local src spec
  if [ -n "${KIT_SOURCE:-}" ]; then
    echo "[3/4] Using local copy $KIT_SOURCE"
    src="$(cd "$KIT_SOURCE" && pwd)"
    spec="$src"
  else
    echo "[3/4] Downloading kit ($repo@$ref)"
    curl -fsSL "https://codeload.github.com/$repo/tar.gz/refs/heads/$ref" | tar -xz -C "$tmp"
    src="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    spec="github:$repo@$ref"
  fi

  echo "[4/4] Installing kit"
  export KIT_HOME="$kit_home"
  # curl | bash leaves stdin as the pipe, so prompts read from the terminal
  if [ -t 0 ] || [ -n "${KIT_NONINTERACTIVE:-}" ]; then
    PYTHONPATH="$src" "$py" -m kit.cli setup --source "$spec" --from "$src"
  elif (exec </dev/tty) 2>/dev/null; then
    PYTHONPATH="$src" "$py" -m kit.cli setup --source "$spec" --from "$src" </dev/tty
  else
    KIT_NONINTERACTIVE=1 PYTHONPATH="$src" "$py" -m kit.cli setup --source "$spec" --from "$src"
  fi
}

main "$@"
