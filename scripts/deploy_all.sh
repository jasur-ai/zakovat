#!/usr/bin/env bash
# One-shot Vercel deploy + webhook registration.
# Run locally with your own tokens exported in the shell. Never commit them.
set -euo pipefail

: "${VERCEL_TOKEN:?VERCEL_TOKEN kerak}"
: "${VERCEL_ORG_ID:?VERCEL_ORG_ID kerak}"
: "${VERCEL_PROJECT_ID:?VERCEL_PROJECT_ID kerak}"
: "${BOT_TOKEN:?BOT_TOKEN kerak}"
: "${WEBHOOK_SECRET:?WEBHOOK_SECRET kerak}"

VC="npx --yes vercel@latest"

$VC pull --yes --environment=production --token="$VERCEL_TOKEN"
printf '%s' "$BOT_TOKEN"      | $VC env add BOT_TOKEN production --yes --token="$VERCEL_TOKEN"
printf '%s' "$WEBHOOK_SECRET" | $VC env add WEBHOOK_SECRET production --yes --token="$VERCEL_TOKEN"

$VC build --prod --token="$VERCEL_TOKEN"
URL=$($VC deploy --prebuilt --prod --token="$VERCEL_TOKEN" | tail -1)
export WEBHOOK_URL="$URL"
python3 scripts/set_webhook.py
echo "Deploy tayyor: $URL"
