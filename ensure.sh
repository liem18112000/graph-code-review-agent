#!/usr/bin/env sh
# Bootstrap for macOS / Linux / Git-Bash-on-Windows.
#
# ensure.py cannot check whether Python exists -- it needs Python to run. This
# shim is the only part that must work with nothing installed, so it is POSIX sh
# and does exactly one thing: find a Python >= 3.11, then hand off.
#
#   sh ensure.sh            fast checks
#   sh ensure.sh --deep     also probe the tier-1 endpoint and Claude auth
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REQ="3.11"

ok() { "$1" -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)' \
        >/dev/null 2>&1; }

PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3 python; do
  command -v "$c" >/dev/null 2>&1 && ok "$c" && { PY=$c; break; }
done

if [ -z "$PY" ]; then
  echo "MUST: Python >= $REQ not found."
  echo
  found=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
  [ -n "$found" ] && echo "  (found $found, but it is older than $REQ -- tomllib is stdlib from 3.11)"
  case "$(uname -s)" in
    Darwin) echo "  brew install python@3.12" ;;
    Linux)  echo "  sudo apt install python3.12        # Debian/Ubuntu"
            echo "  sudo dnf install python3.12        # Fedora"
            echo "  sudo pacman -S python              # Arch" ;;
    *)      echo "  winget install Python.Python.3.12  # Windows" ;;
  esac
  echo
  echo "Then re-run: sh ensure.sh"
  exit 1
fi

exec "$PY" "$DIR/ensure.py" "$@"
