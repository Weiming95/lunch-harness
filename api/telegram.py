"""
Vercel function for lunch-harness — one handler serves everything.

State lives in Upstash Redis (via harness), so nothing is committed to git.

Routes (single entrypoint via pyproject [tool.vercel] entrypoint):
  POST /api/telegram  — Telegram webhook: verify secret, run the agent, reply
  GET  /api/suggest   — Vercel Cron: run the daily lunch suggestion (verify CRON_SECRET)
  GET  /              — live dashboard, rendered from Redis on every request

Env: OPENCODE_API_KEY, OPENCODE_MODEL, GOOGLE_PLACES_API_KEY, TELEGRAM_BOT_TOKEN,
     TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET, CRON_SECRET, DAILY_CALORIE_TARGET,
     UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import sys

# Make the repo root importable; harness reads its config from env at import.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("LUNCH_DATA_DIR", "/tmp/lh/data")
os.environ.setdefault("LUNCH_DOCS_DIR", "/tmp/lh/docs")

import harness  # noqa: E402

WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def process_update(update):
    """Handle one Telegram update. State reads/writes go to Redis via harness."""
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return "no-text"
    chat_id = str(msg["chat"]["id"])
    if harness.TELEGRAM_CHAT_ID and chat_id != harness.TELEGRAM_CHAT_ID:
        return "ignored-chat"
    text = msg["text"].strip()
    if not text:
        return "empty"

    # Over a webhook the send_telegram tool call is the only channel back to the
    # user; if the model answered with plain text, forward it ourselves.
    messages = [harness._system_message()]
    reply = harness.converse(messages, text)
    if not harness._last_telegram_text(messages):
        harness.send_telegram(reply or "Got it ✅")
    return "handled"


class handler(BaseHTTPRequestHandler):
    def _respond(self, code=200, body="ok", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        # Vercel Cron -> daily lunch suggestion.
        if path.endswith("/api/suggest"):
            if CRON_SECRET and self.headers.get("Authorization") != f"Bearer {CRON_SECRET}":
                self._respond(401, "unauthorized")
                return
            # Weekdays only, regardless of cron scheduling precision.
            if harness._now_sgt().weekday() >= 5:
                self._respond(200, "weekend-skip")
                return
            try:
                harness.suggest()
            except Exception as e:
                self._respond(200, f"suggest error: {e}")
                return
            self._respond(200, "suggested")
            return

        # Everything else -> live dashboard.
        try:
            html = harness.build_dashboard_html(harness.load_food_log(), harness.load_suggestions())
        except Exception as e:
            self._respond(200, f"dashboard error: {e}")
            return
        self._respond(200, html, ctype="text/html")

    def do_POST(self):
        if WEBHOOK_SECRET and self.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        ) != WEBHOOK_SECRET:
            self._respond(401, "unauthorized")
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            update = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._respond(200, "bad-json")
            return

        try:
            status = process_update(update)
        except Exception as e:  # never 500 — Telegram would retry-storm us
            try:
                harness.send_telegram("Sorry, I hit an error handling that. Try again in a bit.")
            except Exception:
                pass
            self._respond(200, f"error: {e}")
            return

        self._respond(200, status)
