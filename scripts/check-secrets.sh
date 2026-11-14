#!/usr/bin/env bash
# Blocks commits that contain secrets or anything specific to one person's setup.
#   scripts/check-secrets.sh            scan every tracked file
#   scripts/check-secrets.sh --staged   scan staged changes (pre-commit hook)
# Runs gitleaks too when it's installed.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

if [ "${1:-}" = "--staged" ]; then
  files=$(git diff --cached --name-only --diff-filter=ACM)
else
  files=$(git ls-files)
fi
files=$(printf '%s\n' "$files" | grep -v '^scripts/check-secrets.sh$' || true)
[ -z "$files" ] && exit 0

patterns=(
  '\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b'                 # private 10.x
  '\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b'                       # private 192.168.x
  '\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b'     # private 172.16-31.x
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'  # Tailscale / CGNAT
  '[a-z0-9-]+\.ts\.net\b'                                     # Tailscale MagicDNS
  '\btk_[A-Za-z0-9]{20,}'                                     # ntfy tokens
  '\bgh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}' # GitHub tokens
  'sk-(ant-)?[A-Za-z0-9_-]{30,}'                              # API keys
  'AKIA[0-9A-Z]{16}'                                          # AWS keys
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  '[A-Za-z0-9._%+-]+@(gmail|outlook|hotmail|icloud|yahoo|live)\.[a-z.]+'  # personal emails
)

bad=0
for p in "${patterns[@]}"; do
  # shellcheck disable=SC2086
  hits=$(printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -nIEo -- "$p" 2>/dev/null || true)
  if [ -n "$hits" ]; then
    echo "Blocked: matches /$p/"
    printf '%s\n' "$hits" | sed 's/^/  /'
    bad=1
  fi
done

if command -v gitleaks >/dev/null; then
  if [ "${1:-}" = "--staged" ]; then
    gitleaks git --staged --no-banner --redact -v >/dev/null 2>&1 || { echo "Blocked: gitleaks found a secret (run: gitleaks git --staged -v)"; bad=1; }
  else
    gitleaks dir . --no-banner --redact >/dev/null 2>&1 || { echo "Blocked: gitleaks found a secret (run: gitleaks dir . -v)"; bad=1; }
  fi
fi

if [ "$bad" = 1 ]; then
  echo
  echo "Modules must ask for setup-specific values (IPs, hostnames, tokens), never hardcode them."
  exit 1
fi
exit 0
