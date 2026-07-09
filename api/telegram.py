"""
Vercel serverless function: Telegram webhook for lunch-harness.

Telegram POSTs each message here the instant it arrives, so replies are
instant (no polling). We reuse harness.py for the agent + tools, keep state in
the GitHub repo (so GitHub Pages keeps working), and use the function's /tmp as
scratch because the Vercel filesystem is otherwise read-only.

Flow per message:
  1. verify Telegram's secret-token header
  2. read data/food_log.json from GitHub  -> seed /tmp
  3. run harness.converse() (logs the meal to /tmp, replies via send_telegram)
  4. if the log changed, commit it back to GitHub (which triggers dashboard.yml)

Required Vercel env vars:
  OPENCODE_API_KEY, OPENCODE_MODEL, GOOGLE_PLACES_API_KEY,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET,
  GITHUB_TOKEN (fine-grained PAT, contents:write on the repo),
  GITHUB_REPO (e.g. "Weiming95/lunch-harness"), GITHUB_BRANCH (default "main"),
  LUNCH_DATA_DIR=/tmp/lh/data, LUNCH_DOCS_DIR=/tmp/lh/docs
"""

from http.server import BaseHTTPRequestHandler
import base64
import json
import os
import sys
import urllib.request
import urllib.error

# Make the repo root importable and point harness's state at writable /tmp
# BEFORE importing harness (its path constants are computed at import time).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("LUNCH_DATA_DIR", "/tmp/lh/data")
os.environ.setdefault("LUNCH_DOCS_DIR", "/tmp/lh/docs")

import harness  # noqa: E402

GH_REPO = os.environ.get("GITHUB_REPO", "")
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
FOOD_LOG_REPO_PATH = "data/food_log.json"


# --- GitHub Contents API helpers ------------------------------------------
def _gh_api(method, path, payload=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lunch-harness-webhook",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}


def gh_get_food_log():
    """Return (content_str, sha). sha is None if the file doesn't exist yet."""
    try:
        resp = _gh_api("GET", f"/repos/{GH_REPO}/contents/{FOOD_LOG_REPO_PATH}?ref={GH_BRANCH}")
        return base64.b64decode(resp["content"]).decode(), resp["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "[]\n", None
        raise


def gh_get_json(path, default):
    """Fetch and parse a JSON file from the repo (default on 404)."""
    try:
        resp = _gh_api("GET", f"/repos/{GH_REPO}/contents/{path}?ref={GH_BRANCH}")
        return json.loads(base64.b64decode(resp["content"]).decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return default
        raise


def gh_put_food_log(content, sha, message):
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": GH_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    return _gh_api("PUT", f"/repos/{GH_REPO}/contents/{FOOD_LOG_REPO_PATH}", payload)


def _seed_tmp(food_log_content):
    os.makedirs(harness.DATA_DIR, exist_ok=True)
    with open(harness.FOOD_LOG, "w") as f:
        f.write(food_log_content)
    # render_dashboard/read paths also touch suggestions.json — keep it valid.
    if not os.path.exists(harness.SUGGESTIONS):
        with open(harness.SUGGESTIONS, "w") as f:
            f.write("[]\n")


# --- Core (testable, no HTTP framework) -----------------------------------
def process_update(update, commit=True):
    """Handle one Telegram update. Returns a short status string."""
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return "no-text"

    chat_id = str(msg["chat"]["id"])
    if harness.TELEGRAM_CHAT_ID and chat_id != harness.TELEGRAM_CHAT_ID:
        return "ignored-chat"

    text = msg["text"].strip()
    if not text:
        return "empty"

    content, sha = gh_get_food_log()
    _seed_tmp(content)

    # Runs the agent: logs/corrects the meal in /tmp and (usually) replies via
    # send_telegram. Over a webhook the tool call is the ONLY way back to the
    # user, so if the model answered with plain text instead of calling the
    # tool, forward that text ourselves — otherwise the user gets silence.
    messages = [harness._system_message()]
    reply = harness.converse(messages, text)
    if not harness._last_telegram_text(messages):
        harness.send_telegram(reply or "Got it ✅")

    with open(harness.FOOD_LOG) as f:
        new_content = f.read()

    if commit and new_content != content:
        gh_put_food_log(new_content, sha, f"log via telegram: {text[:60]}")
        return "logged+committed"
    return "handled" if new_content == content else "logged(no-commit)"


# --- Vercel handler --------------------------------------------------------
class handler(BaseHTTPRequestHandler):
    def _respond(self, code=200, body="ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        # Live dashboard: render the current GitHub state on every request, so
        # it's always up to date (no waiting on the Actions -> Pages rebuild).
        try:
            log = gh_get_json("data/food_log.json", [])
            suggestions = gh_get_json("data/suggestions.json", [])
            html = harness.build_dashboard_html(log, suggestions)
        except Exception as e:
            self._respond(200, f"dashboard error: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html.encode())

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
