#!/usr/bin/env bash
# Runs inside a GitHub Codespace (which has unrestricted internet access).
# Exports the full Zakovat question bank and pushes the JSON shards back
# to the branch this codespace was created from.
set -euo pipefail

BRANCH="$(git branch --show-current)"
echo "Exporting full question bank on branch: $BRANCH"

python3 scripts/export_all.py --out-dir data/all 2>&1 | tee data/all-export.log

git config user.name "Arena Agent"
git config user.email "agent@users.noreply.github.com"

git add data/all data/all-export.log
if git diff --cached --quiet; then
  echo "No new data to commit."
  exit 0
fi

git commit -m "Import full Zakovat question bank from public API"
git push origin "HEAD:$BRANCH"
echo "Full question bank pushed to $BRANCH"
