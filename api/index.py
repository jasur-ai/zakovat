"""Vercel Python entrypoint: FastAPI ASGI app with Telegram webhook + Mini App."""
from bot.webhook import app as _app

# Vercel's Python analyzer looks for a top-level `app` variable.
app = _app
