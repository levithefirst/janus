#!/usr/bin/env bash
# One command from a fresh clone: ./setup.sh
# Creates .venv, installs dependencies, writes .env with a fresh JANUS_SECRET (never overwrites an
# existing .env), and runs the test suite. Works offline afterwards: PayPal and the AI run as mocks
# until you put sandbox credentials in .env.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10 or newer is required (found $("$PY" --version 2>&1))"; exit 1; }

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  secret="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  sed "s|^JANUS_SECRET=.*|JANUS_SECRET=${secret}|" .env.example > .env
  chmod 600 .env
  echo "Wrote .env with a fresh JANUS_SECRET (it is gitignored; keep it out of version control)."
fi

.venv/bin/python -m pytest -q -p no:cacheprovider
echo
echo "Ready. Activate with: source .venv/bin/activate"
echo "  python -m janus demo     # match / mismatch / unclear, end to end (mock PayPal and AI)"
echo "  python -m janus serve    # web app + API on http://localhost:8000"
