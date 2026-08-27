#!/usr/bin/env sh
# PyMolt installer — https://pymolt.zeelex.me/install.sh
#
#   curl -fsSL https://pymolt.zeelex.me/install.sh | sh
#
# PyMolt is a Python CLI (requires Python 3.12+). This script installs it as an
# isolated tool via `uv` — uv provisions a matching Python for you, so you don't
# need to have 3.12 already. If uv is missing, we bootstrap it first (Astral's
# official installer). Nothing is installed into your global/system Python.
#
# Overrides (env vars):
#   PYMOLT_VERSION=0.1.0     pin a specific release (default: latest on PyPI)
#   PYMOLT_INSTALLER=uv|pipx|pip   force an installer (default: auto-detect)
set -eu

PACKAGE="pymolt"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"

# ---------------------------------------------------------------------------
# Output helpers (color only when stdout is a TTY)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"; RED="$(printf '\033[31m')"
  GREEN="$(printf '\033[32m')"; YELLOW="$(printf '\033[33m')"; RESET="$(printf '\033[0m')"
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

info() { printf '%s\n' "$*"; }
step() { printf '%s→%s %s\n' "$BOLD" "$RESET" "$*"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
err()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# curl wrapper that retries transient network failures. Flags kept portable to
# old curl (avoid --retry-all-errors, which is curl 7.71+).
curl_retry() {
  curl --fail --silent --show-error --location \
    --retry 5 --retry-delay 2 --retry-max-time 120 --connect-timeout 10 "$@"
}

# ---------------------------------------------------------------------------
# Preflight: OS (for messaging) + a downloader
# ---------------------------------------------------------------------------
OS="$(uname -s 2>/dev/null || echo unknown)"
case "$OS" in
  Darwin) PLATFORM="macOS" ;;
  Linux)  PLATFORM="Linux" ;;
  *)      PLATFORM="$OS"; warn "Unrecognized OS '$OS' — proceeding, but only macOS and Linux are tested." ;;
esac

if ! have curl && ! have wget; then
  err "need curl or wget to bootstrap. Install one (e.g. your package manager) and re-run."
fi

info "${BOLD}Installing PyMolt${RESET} ${DIM}(${PLATFORM})${RESET}"

# ---------------------------------------------------------------------------
# Bootstrap uv if we'll use it (default installer). uv brings its own managed
# Python, satisfying PyMolt's 3.12+ requirement without touching system Python.
# ---------------------------------------------------------------------------
ensure_uv() {
  if have uv; then
    return 0
  fi
  step "Installing uv (Astral) — the Python toolchain PyMolt uses"
  if have curl; then
    curl_retry "$UV_INSTALL_URL" | sh
  else
    wget -qO- "$UV_INSTALL_URL" | sh
  fi
  # uv installs to ~/.local/bin (or $XDG_BIN_HOME / $CARGO_HOME/bin); make it
  # visible to THIS script's remaining steps even before the user's PATH updates.
  for d in "$HOME/.local/bin" "${XDG_BIN_HOME:-}" "${CARGO_HOME:-$HOME/.cargo}/bin"; do
    [ -n "$d" ] && [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH" ;; esac
  done
  export PATH
  have uv || err "uv installed but not on PATH — open a new shell and re-run this installer."
}

# ---------------------------------------------------------------------------
# Choose installer and install
# ---------------------------------------------------------------------------
# Install with the `mcp` extra so `pymolt mcp` (the stdio MCP server) works
# out of the box. The extra goes between the name and any version specifier:
# pymolt[mcp] or pymolt[mcp]==0.1.0. If PYMOLT_SOURCE is set (e.g. git URL), use that.
if [ -n "${PYMOLT_SOURCE:-}" ]; then
  SPEC="$PYMOLT_SOURCE"
else
  SPEC_BASE="$PACKAGE[mcp]"
  SPEC="$SPEC_BASE"
  if [ -n "${PYMOLT_VERSION:-}" ]; then
    SPEC="$SPEC_BASE==$PYMOLT_VERSION"
  fi
fi

installer="${PYMOLT_INSTALLER:-}"
if [ -z "$installer" ]; then
  if have uv; then installer="uv"
  elif have pipx; then installer="pipx"
  else installer="uv"; fi   # no uv, no pipx → bootstrap uv (best 3.12+ story)
fi

case "$installer" in
  uv)
    ensure_uv
    step "Installing $SPEC via uv"
    uv tool install --upgrade "$SPEC"
    BIN_HINT="$(uv tool dir 2>/dev/null || echo "$HOME/.local/bin")"
    ;;
  pipx)
    have pipx || err "PYMOLT_INSTALLER=pipx but pipx is not installed."
    step "Installing $SPEC via pipx"
    pipx install --force "$SPEC"
    BIN_HINT="$HOME/.local/bin"
    ;;
  pip)
    have python3 || err "PYMOLT_INSTALLER=pip but python3 is not installed."
    step "Installing $SPEC via pip (user site)"
    python3 -m pip install --user --upgrade "$SPEC"
    BIN_HINT="$(python3 -m site --user-base 2>/dev/null)/bin"
    ;;
  *)
    err "unknown PYMOLT_INSTALLER='$installer' (expected uv | pipx | pip)."
    ;;
esac

# ---------------------------------------------------------------------------
# Verify + PATH guidance
# ---------------------------------------------------------------------------
if have pymolt; then
  installed_version="$(pymolt --version 2>/dev/null || echo "")"
  info ""
  info "${GREEN}✓ PyMolt installed${RESET} ${DIM}${installed_version}${RESET}"
  info "  Try it:  ${BOLD}pymolt scan .${RESET}"
else
  info ""
  warn "PyMolt was installed but 'pymolt' is not on your PATH yet."
  info "  Add its bin directory to PATH, then restart your shell:"
  info "    ${BOLD}export PATH=\"$BIN_HINT:\$PATH\"${RESET}"
  if [ "$installer" = "uv" ]; then
    info "  (or run ${BOLD}uv tool update-shell${RESET} to have uv configure PATH for you)"
  fi
fi
