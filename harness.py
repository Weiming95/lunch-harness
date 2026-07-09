#!/usr/bin/env python3
"""
lunch-harness — a personal lunch suggester + calorie tracker in one file.

Pivoted from https://github.com/Weiming95/hadr-harness : same idea (the harness
is the loop, the tools, and the interface; the model is just a text-in/text-out
function) but for lunch near 1 Depot Rd, Singapore.

The model (OpenCode Go, OpenAI-compatible) drives a tool-calling loop. Tools let
it search nearby places (Google Places API v1), read/append/correct a food log,
and message the user on Telegram. State lives in ./data/*.json and is committed
back to the repo by GitHub Actions, which also serves ./docs as GitHub Pages.

Stdlib only — no pip installs.

Modes:
    python3 harness.py                 # interactive REPL (debug)
    python3 harness.py --once "..."    # one-shot prompt (debug)
    python3 harness.py --poll          # drain Telegram, log/correct meals, rebuild dashboard
    python3 harness.py --suggest       # daily lunch suggestion run
    python3 harness.py --dashboard     # just re-render docs/index.html from data
    python3 harness.py --tg-updates    # print recent Telegram updates + chat ids
    python3 harness.py --models        # list available models
"""

import json
import math
import os
import sys
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
# Data/docs dirs are override-able so the Vercel webhook can point them at the
# function's writable /tmp; locally and in Actions they default to the repo.
DATA_DIR = os.environ.get("LUNCH_DATA_DIR", os.path.join(ROOT, "data"))
DOCS_DIR = os.environ.get("LUNCH_DOCS_DIR", os.path.join(ROOT, "docs"))
FOOD_LOG = os.path.join(DATA_DIR, "food_log.json")
SUGGESTIONS = os.path.join(DATA_DIR, "suggestions.json")
TG_OFFSET = os.path.join(DATA_DIR, "tg_offset.json")
SYSTEM_PROMPT_FILE = os.path.join(ROOT, "system_prompt.md")

# Singapore is UTC+8 (no DST).
SGT = timezone(timedelta(hours=8))

# 1 Depot Road, Singapore 109679 (Defence Technology Tower A).
OFFICE_LAT = 1.27930
OFFICE_LNG = 103.81655
SEARCH_RADIUS_M = 1500  # walking distance; results beyond this are dropped

# ---------------------------------------------------------------------------
# Config (env -> .env -> defaults), mirroring the reference harness.
# ---------------------------------------------------------------------------
def _load_dotenv():
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()


def _read_api_key():
    """OPENCODE_API_KEY from env, else macOS Keychain (generic password
    'opencode-api-key'). Returns '' if unavailable."""
    key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if key:
        return key
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "opencode-api-key", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


OPENCODE_BASE_URL = os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "kimi-k2")
OPENCODE_API_KEY = _read_api_key()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()

# Upstash Redis (REST). When set, it's the state store; otherwise fall back to
# local JSON files (for local dev / the CLI). The Vercel Upstash integration
# provides UPSTASH_REDIS_REST_* (and KV_REST_API_* aliases).
REDIS_URL = (os.environ.get("UPSTASH_REDIS_REST_URL")
             or os.environ.get("KV_REST_API_URL") or "").rstrip("/")
REDIS_TOKEN = (os.environ.get("UPSTASH_REDIS_REST_TOKEN")
               or os.environ.get("KV_REST_API_TOKEN") or "")

DAILY_CALORIE_TARGET = int(os.environ.get("DAILY_CALORIE_TARGET", "2100"))

MAX_TOOL_ITERS = 12


# ---------------------------------------------------------------------------
# Small JSON store helpers
# ---------------------------------------------------------------------------
def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _now_sgt():
    return datetime.now(SGT)


def _today_sgt():
    return _now_sgt().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# State store: Upstash Redis (REST) when configured, else local JSON files.
# Keeps the food log + suggestions off git — no commit per meal.
# ---------------------------------------------------------------------------
def _redis_get(key):
    req = urllib.request.Request(
        f"{REDIS_URL}/get/{urllib.parse.quote(key, safe='')}",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}", "User-Agent": "lunch-harness/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode()).get("result")


def _redis_set(key, value_str):
    req = urllib.request.Request(
        f"{REDIS_URL}/set/{urllib.parse.quote(key, safe='')}",
        data=value_str.encode(), method="POST",
        headers={"Authorization": f"Bearer {REDIS_TOKEN}", "User-Agent": "lunch-harness/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


_LOCAL_PATHS = {
    "food_log": FOOD_LOG,
    "suggestions": SUGGESTIONS,
    "pending": os.path.join(DATA_DIR, "pending.json"),
}


def _store_get(key, default):
    if REDIS_URL and REDIS_TOKEN:
        val = _redis_get(key)
        return json.loads(val) if val else default
    return _read_json(_LOCAL_PATHS[key], default)


def _store_set(key, value):
    if REDIS_URL and REDIS_TOKEN:
        _redis_set(key, json.dumps(value, ensure_ascii=False))
    else:
        _write_json(_LOCAL_PATHS[key], value)


def load_food_log():
    return _store_get("food_log", [])


def save_food_log(log):
    _store_set("food_log", log)


def load_suggestions():
    return _store_get("suggestions", [])


def save_suggestions(suggestions):
    _store_set("suggestions", suggestions)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _post(url, payload, headers=None):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", "User-Agent": "lunch-harness/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error calling {url}: {e}") from None


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lunch-harness/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Model call (OpenAI-compatible chat/completions with tools)
# ---------------------------------------------------------------------------
def call_model(messages):
    if not OPENCODE_API_KEY:
        raise RuntimeError("OPENCODE_API_KEY is not set (env, .env, or Keychain).")
    # NB: the Console Go provider 400s when `temperature` is sent alongside
    # `tools` for these models, so we omit it and take the model default.
    payload = {
        "model": OPENCODE_MODEL,
        "messages": messages,
        "tools": TOOLS,
        "max_tokens": 2048,
    }
    resp = _post(
        f"{OPENCODE_BASE_URL}/chat/completions",
        payload,
        headers={"Authorization": f"Bearer {OPENCODE_API_KEY}"},
    )
    return resp["choices"][0]["message"]


def list_models():
    try:
        resp = _get(f"{OPENCODE_BASE_URL}/models")
        for m in resp.get("data", resp if isinstance(resp, list) else []):
            print(m.get("id", m))
    except Exception as e:
        print(f"Could not list models: {e}")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
_PRICE_LABELS = {
    "PRICE_LEVEL_FREE": "free",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


def _haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def search_places(keyword="lunch", max_results=8):
    """Find eateries within walking distance of the office via Google Places
    API v1 (searchText). Text Search's locationRestriction only supports
    rectangles, so we bias to a circle, then hard-filter by real distance and
    sort nearest-first — keeping results actually walkable from 1 Depot Rd."""
    if not GOOGLE_PLACES_API_KEY:
        return {"error": "GOOGLE_PLACES_API_KEY not set."}
    max_results = max(1, min(int(max_results or 8), 15))
    payload = {
        "textQuery": f"{keyword} restaurants",
        "maxResultCount": 20,  # over-fetch; we filter by distance below
        "locationBias": {
            "circle": {
                "center": {"latitude": OFFICE_LAT, "longitude": OFFICE_LNG},
                "radius": float(SEARCH_RADIUS_M),
            }
        },
    }
    field_mask = ",".join([
        "places.displayName",
        "places.location",
        "places.rating",
        "places.userRatingCount",
        "places.priceLevel",
        "places.primaryTypeDisplayName",
        "places.editorialSummary",
        "places.formattedAddress",
    ])
    try:
        resp = _post(
            "https://places.googleapis.com/v1/places:searchText",
            payload,
            headers={
                "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": field_mask,
            },
        )
    except RuntimeError as e:
        return {"error": str(e)}

    results = []
    for p in resp.get("places", []):
        loc = p.get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if lat is None or lng is None:
            continue
        dist = round(_haversine_m(OFFICE_LAT, OFFICE_LNG, lat, lng))
        if dist > SEARCH_RADIUS_M:
            continue
        results.append({
            "name": p.get("displayName", {}).get("text", "?"),
            "type": p.get("primaryTypeDisplayName", {}).get("text", ""),
            "rating": p.get("rating"),
            "ratings_count": p.get("userRatingCount"),
            "price": _PRICE_LABELS.get(p.get("priceLevel", ""), ""),
            "summary": p.get("editorialSummary", {}).get("text", ""),
            "address": p.get("formattedAddress", ""),
            "distance_m": dist,
        })
    results.sort(key=lambda r: r["distance_m"])
    return {"query": keyword, "count": len(results), "places": results[:max_results]}


def read_recent_picks(count=10):
    """Recent lunch suggestions, so you can avoid repeating a place or cuisine."""
    s = load_suggestions()
    count = max(1, min(int(count or 10), 30))
    return {"recent_picks": [x.get("pick", "") for x in s[-count:]]}


def read_food_log(days=3):
    """Return meals logged in the last `days` days (SGT), plus today's total."""
    days = max(1, min(int(days or 3), 30))
    log = load_food_log()
    cutoff = (_now_sgt() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    recent = [m for m in log if m.get("date", "") >= cutoff]
    today = _today_sgt()
    today_cals = sum(int(m.get("calories", 0)) for m in log if m.get("date") == today)
    return {
        "days": days,
        "today": today,
        "today_calories": today_cals,
        "daily_target": DAILY_CALORIE_TARGET,
        "meals": recent,
    }


def log_meal(description, calories, protein_g=None, meal_type=None):
    """Append a meal to the food log (dated now, SGT)."""
    log = load_food_log()
    now = _now_sgt()
    entry = {
        "id": (log[-1]["id"] + 1) if log else 1,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "description": str(description),
        "calories": int(calories),
        "protein_g": int(protein_g) if protein_g is not None else None,
        "meal_type": meal_type or _infer_meal_type(now),
    }
    log.append(entry)
    save_food_log(log)
    today_cals = sum(int(m.get("calories", 0)) for m in log if m.get("date") == entry["date"])
    return {"logged": entry, "today_calories": today_cals, "daily_target": DAILY_CALORIE_TARGET}


def update_last_meal(calories=None, description=None, protein_g=None):
    """Correct the most recently logged meal."""
    log = load_food_log()
    if not log:
        return {"error": "food log is empty; nothing to update."}
    entry = log[-1]
    if calories is not None:
        entry["calories"] = int(calories)
    if description is not None:
        entry["description"] = str(description)
    if protein_g is not None:
        entry["protein_g"] = int(protein_g)
    save_food_log(log)
    today_cals = sum(int(m.get("calories", 0)) for m in log if m.get("date") == entry["date"])
    return {"updated": entry, "today_calories": today_cals}


def delete_last_meal():
    """Remove the most recently logged meal."""
    log = load_food_log()
    if not log:
        return {"error": "food log is empty; nothing to delete."}
    removed = log.pop()
    save_food_log(log)
    return {"deleted": removed}


def send_telegram(message, buttons=None):
    """Send a message to the configured chat. `buttons` is an optional list of
    rows, each row a list of (label, callback_data) tuples, rendered as an
    inline keyboard."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"status": "telegram not configured; message not sent", "message": message}
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]
        }
    try:
        _post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", payload)
        return {"status": "sent"}
    except RuntimeError as e:
        return {"status": f"error: {e}"}


def answer_callback(callback_id, text=""):
    """Acknowledge a button tap so Telegram stops the loading spinner."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        _post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text},
        )
    except RuntimeError:
        pass


def remove_buttons(message_id):
    """Strip the inline keyboard off a message (e.g. after it's been acted on)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return
    try:
        _post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
            {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )
    except RuntimeError:
        pass


def _infer_meal_type(dt):
    h = dt.hour
    if h < 11:
        return "breakfast"
    if h < 15:
        return "lunch"
    if h < 18:
        return "snack"
    return "dinner"


# ---------------------------------------------------------------------------
# Confirm workflow: a "pending" proposal (a meal or a lunch pick) waits in the
# store until the user confirms (tap or "yes") or adjusts/cancels. Nothing hits
# the food log until confirmed.
# ---------------------------------------------------------------------------
def load_pending():
    return _store_get("pending", None)


def save_pending(obj):
    _store_set("pending", obj)


def clear_pending():
    _store_set("pending", None)


def _today_total():
    today = _today_sgt()
    return sum(int(m.get("calories", 0)) for m in load_food_log() if m.get("date") == today)


def propose_meal(description, calories, protein_g=None, meal_type=None, note=None):
    """Propose logging a meal (DON'T log yet). Sends the estimate + Confirm/Cancel
    buttons and stores it as pending. Use this instead of logging directly."""
    calories = int(calories)
    pending = {
        "kind": "meal",
        "description": str(description),
        "calories": calories,
        "protein_g": int(protein_g) if protein_g is not None else None,
        "meal_type": meal_type or _infer_meal_type(_now_sgt()),
    }
    save_pending(pending)
    macro = f", ~{int(protein_g)}g protein" if protein_g is not None else ""
    projected = _today_total() + calories
    msg = (f"{description} — I estimate ~{calories} kcal{macro}. "
           f"That would put you at {projected} / {DAILY_CALORIE_TARGET} today.")
    if note:
        msg += f"\n{note}"
    msg += "\n\nLog it?"
    send_telegram(msg, buttons=[[("✅ Confirm", "confirm"), ("✖️ Cancel", "cancel")]])
    return {"status": "proposed", "pending": pending}


def propose_pick(place, dish, calories, note=None):
    """Propose a lunch pick (DON'T log yet). Sends it with Ate-it / Suggest-another
    buttons, stores it as pending, and records it so future picks stay varied."""
    calories = int(calories)
    desc = f"{dish} at {place}"
    save_pending({"kind": "pick", "description": desc, "calories": calories,
                  "protein_g": None, "meal_type": "lunch"})
    # Record the pick for variety + the dashboard, even before it's confirmed.
    suggestions = load_suggestions()
    suggestions.append({"date": _today_sgt(), "pick": f"{desc} (~{calories} kcal)", "reason": note or ""})
    save_suggestions(suggestions)
    msg = f"🍴 Lunch pick: {dish} at {place} — ~{calories} kcal."
    if note:
        msg += f"\n{note}"
    msg += "\n\nHad it? (Or just tell me what you actually ate.)"
    send_telegram(msg, buttons=[
        [("✅ Ate it", "confirm"), ("🔄 Suggest another", "another")],
        [("✍️ Ate something else", "other")],
    ])
    return {"status": "pick proposed"}


def confirm_pending():
    """Log the pending proposal to the food log and clear it. Call this when the
    user confirms/accepts (e.g. says yes)."""
    pending = load_pending()
    if not pending:
        send_telegram("Nothing to confirm right now — tell me what you ate and I'll estimate it.")
        return {"status": "no pending"}
    result = log_meal(pending["description"], pending["calories"],
                      pending.get("protein_g"), pending.get("meal_type"))
    clear_pending()
    e = result["logged"]
    total = result["today_calories"]
    send_telegram(f"Logged {e['description']} — {e['calories']} kcal "
                  f"({total} / {DAILY_CALORIE_TARGET} today). ✅")
    return {"status": "logged", "logged": e, "today_calories": total}


def cancel_pending():
    """Discard the pending proposal without logging anything."""
    if not load_pending():
        return {"status": "nothing pending"}
    clear_pending()
    send_telegram("Okay — cancelled, nothing logged.")
    return {"status": "cancelled"}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_places",
            "description": "Search for eateries near the office (1 Depot Rd, Singapore) using Google Places. Use for lunch suggestions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Cuisine or dish, e.g. 'healthy', 'japanese', 'salad', 'chicken rice'."},
                    "max_results": {"type": "integer", "description": "How many places to return (1-15)."},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_recent_picks",
            "description": "List recently suggested lunch picks so you can suggest something different and avoid repeats.",
            "parameters": {
                "type": "object",
                "properties": {"count": {"type": "integer", "description": "How many recent picks to return (default 10)."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_food_log",
            "description": "Read meals logged over the last N days, plus today's calorie total and the daily target. Use before suggesting lunch or judging how the day is going.",
            "parameters": {
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "Look-back window in days (default 3)."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_meal",
            "description": "Propose logging a meal the user reported — estimate its calories (and protein if you can) and present it for confirmation. This does NOT log yet; it shows the user Confirm/Cancel buttons. Use this for every meal the user mentions; also call it again with new numbers when they adjust ('make it 700').",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What was eaten, e.g. 'chicken rice and kopi'."},
                    "calories": {"type": "integer", "description": "Estimated total calories (use the user's number if they gave one)."},
                    "protein_g": {"type": "integer", "description": "Estimated protein in grams (optional)."},
                    "meal_type": {"type": "string", "description": "breakfast | lunch | snack | dinner (optional)."},
                    "note": {"type": "string", "description": "Optional one-line note, e.g. a quick macro breakdown."},
                },
                "required": ["description", "calories"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_pick",
            "description": "Propose ONE lunch pick to the user. Sends it with 'Ate it' / 'Suggest another' buttons so they can accept without re-typing. Use this to deliver every lunch suggestion (instead of send_telegram).",
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "The eatery name."},
                    "dish": {"type": "string", "description": "The specific dish/order to get."},
                    "calories": {"type": "integer", "description": "Rough calorie estimate for that dish."},
                    "note": {"type": "string", "description": "One line on why it fits (health/variety/calories)."},
                },
                "required": ["place", "dish", "calories"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_pending",
            "description": "Log the pending proposal (meal or lunch pick) to the food log. Call this when the user confirms or accepts it (e.g. 'yes', 'ate it', 'go ahead').",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_pending",
            "description": "Discard the pending proposal without logging anything (e.g. user says 'no', 'cancel', 'didn't eat it').",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_last_meal",
            "description": "Correct the most recently logged meal when the user adjusts it (e.g. 'make it 700' or fixes the description).",
            "parameters": {
                "type": "object",
                "properties": {
                    "calories": {"type": "integer"},
                    "description": {"type": "string"},
                    "protein_g": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_last_meal",
            "description": "Delete the most recently logged meal (e.g. user says 'remove that' or 'I didn't eat it').",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_telegram",
            "description": "Send a concise plain-text reply to the user on Telegram.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    },
]

DISPATCH = {
    "search_places": search_places,
    "read_recent_picks": read_recent_picks,
    "read_food_log": read_food_log,
    "propose_meal": propose_meal,
    "propose_pick": propose_pick,
    "confirm_pending": confirm_pending,
    "cancel_pending": cancel_pending,
    "update_last_meal": update_last_meal,
    "delete_last_meal": delete_last_meal,
    "send_telegram": send_telegram,
}

# Tools that deliver a message to the user (used to detect if we still owe a reply).
SENDING_TOOLS = {"propose_meal", "propose_pick", "confirm_pending", "cancel_pending", "send_telegram"}


def _sent_during(messages):
    """True if any user-facing message was sent during this turn."""
    for m in messages:
        for tc in m.get("tool_calls") or []:
            if tc["function"]["name"] in SENDING_TOOLS:
                return True
    return False


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def _system_message():
    try:
        with open(SYSTEM_PROMPT_FILE) as f:
            content = f.read()
    except FileNotFoundError:
        content = "You are a helpful lunch and calorie assistant."
    context = (
        f"\n\n[runtime context] Today is {_today_sgt()} (SGT). "
        f"Daily calorie target: {DAILY_CALORIE_TARGET} kcal. "
        f"Office: 1 Depot Rd, Singapore."
    )
    pending = load_pending()
    if pending:
        context += (
            f"\n\n[pending {pending['kind']} awaiting the user's confirmation] "
            f"\"{pending['description']}\" (~{pending['calories']} kcal). "
            "If the user confirms/accepts (yes, ate it, go ahead), call confirm_pending. "
            "If they adjust it (e.g. 'make it 700'), call propose_meal again with the new "
            "values. If they tell you they actually ate something DIFFERENT (e.g. 'i had sushi "
            "instead'), call propose_meal for what they really ate — that replaces this pending "
            "item. If they want a different lunch pick, call propose_pick with a new option. "
            "If they decline (no, cancel), call cancel_pending."
        )
    return {"role": "system", "content": content + context}


def converse(messages, user_text):
    """Level-4 loop: keep running tools until the model stops requesting them.
    Returns the final assistant text (may be empty if it only used tools)."""
    messages.append({"role": "user", "content": user_text})
    final_text = ""
    for _ in range(MAX_TOOL_ITERS):
        msg = call_model(messages)
        messages.append(msg)
        if msg.get("content"):
            final_text = msg["content"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            fn = DISPATCH.get(name)
            try:
                result = fn(**args) if fn else {"error": f"unknown tool {name}"}
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", name),
                "content": json.dumps(result, ensure_ascii=False),
            })
    return final_text


# ---------------------------------------------------------------------------
# Dashboard renderer (deterministic; not LLM-driven)
# ---------------------------------------------------------------------------
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_dashboard():
    """Read state from disk, render, and write docs/index.html (for Pages)."""
    html = build_dashboard_html(load_food_log(), load_suggestions())
    os.makedirs(DOCS_DIR, exist_ok=True)
    path = os.path.join(DOCS_DIR, "index.html")
    with open(path, "w") as f:
        f.write(html)
    return path


def build_dashboard_html(log, suggestions):
    """Pure renderer: given the meal log + suggestions, return the HTML string.
    Used both to write docs/index.html and to serve a live view from Vercel."""
    today = _today_sgt()

    today_meals = [m for m in log if m.get("date") == today]
    today_cals = sum(int(m.get("calories", 0)) for m in today_meals)
    pct = min(100, round(today_cals / DAILY_CALORIE_TARGET * 100)) if DAILY_CALORIE_TARGET else 0
    over = today_cals > DAILY_CALORIE_TARGET

    # Last 7 days totals for a simple bar chart.
    daily = {}
    for m in log:
        daily[m.get("date", "?")] = daily.get(m.get("date", "?"), 0) + int(m.get("calories", 0))
    last7 = []
    for i in range(6, -1, -1):
        d = (_now_sgt() - timedelta(days=i)).strftime("%Y-%m-%d")
        last7.append((d, daily.get(d, 0)))
    max_cal = max([c for _, c in last7] + [DAILY_CALORIE_TARGET, 1])

    meal_rows = "".join(
        f"<tr><td>{_esc(m.get('time',''))}</td><td>{_esc(m.get('meal_type',''))}</td>"
        f"<td>{_esc(m.get('description',''))}</td><td class='num'>{int(m.get('calories',0))}</td></tr>"
        for m in today_meals
    ) or "<tr><td colspan='4' class='muted'>Nothing logged yet today.</td></tr>"

    bars = "".join(
        f"<div class='bar'><div class='fill{' over' if c>DAILY_CALORIE_TARGET else ''}' "
        f"style='height:{round(c/max_cal*100)}%'></div>"
        f"<span class='bl'>{d[5:]}</span><span class='bv'>{c}</span></div>"
        for d, c in last7
    )

    sugg_rows = "".join(
        f"<li><b>{_esc(s.get('date',''))}</b> — {_esc(s.get('pick',''))}"
        + (f" <span class='muted'>· {_esc(s.get('reason',''))}</span>" if s.get('reason') else "")
        + "</li>"
        for s in reversed(suggestions[-10:])
    ) or "<li class='muted'>No suggestions yet.</li>"

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lunch Tracker</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         margin: 0; padding: 2rem 1rem; background: #f6f7f9; color: #1a1a2e; }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#12131a; color:#e8e8ef; }}
    .card {{ background:#1c1e28 !important; }} th {{ border-color:#333 !important; }}
    td {{ border-color:#2a2c38 !important; }} }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  .sub {{ color: #888; margin: 0 0 1.5rem; font-size: .9rem; }}
  .card {{ background: #fff; border-radius: 14px; padding: 1.25rem; margin-bottom: 1.25rem;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .total {{ font-size: 2.4rem; font-weight: 700; }}
  .total small {{ font-size: 1rem; font-weight: 400; color:#888; }}
  .meter {{ height: 10px; border-radius: 6px; background: #e6e8ee; overflow: hidden; margin:.6rem 0 .2rem; }}
  .meter > i {{ display:block; height:100%; background:{'#e5484d' if over else '#30a46c'}; width:{pct}%; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th,td {{ text-align: left; padding: .5rem .4rem; border-bottom: 1px solid #eee; }}
  th {{ font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; color:#888; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .muted {{ color: #999; }}
  .chart {{ display: flex; gap: .5rem; align-items: flex-end; height: 140px; }}
  .bar {{ flex:1; display:flex; flex-direction:column; justify-content:flex-end; align-items:center; height:100%; position:relative; }}
  .fill {{ width: 70%; background:#30a46c; border-radius:4px 4px 0 0; min-height:2px; }}
  .fill.over {{ background:#e5484d; }}
  .bl {{ font-size:.65rem; color:#888; margin-top:.3rem; }}
  .bv {{ font-size:.6rem; color:#aaa; }}
  ul {{ margin:.3rem 0; padding-left: 1.1rem; }} li {{ margin:.35rem 0; }}
</style></head><body><div class="wrap">
  <h1>🍱 Lunch Tracker</h1>
  <p class="sub">Calorie log &amp; lunch picks near 1 Depot Rd · updated {_esc(_now_sgt().strftime('%Y-%m-%d %H:%M'))} SGT</p>

  <div class="card">
    <div class="total">{today_cals} <small>/ {DAILY_CALORIE_TARGET} kcal today</small></div>
    <div class="meter"><i></i></div>
    <div class="muted">{pct}% of target{' · over target' if over else ''}</div>
    <table style="margin-top:1rem">
      <tr><th>Time</th><th>Meal</th><th>What</th><th class="num">kcal</th></tr>
      {meal_rows}
    </table>
  </div>

  <div class="card">
    <h3 style="margin:0 0 .8rem">Last 7 days</h3>
    <div class="chart">{bars}</div>
  </div>

  <div class="card">
    <h3 style="margin:0 0 .5rem">Recent lunch suggestions</h3>
    <ul>{sugg_rows}</ul>
  </div>
</div></body></html>
"""
    return html


# ---------------------------------------------------------------------------
# Telegram polling
# ---------------------------------------------------------------------------
def _tg_get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?timeout=0"
    if offset:
        url += f"&offset={offset}"
    return _get(url)


def poll():
    """Drain new Telegram messages; run the agent on each; rebuild dashboard."""
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set; nothing to poll.")
        return
    state = _read_json(TG_OFFSET, {"offset": 0})
    offset = state.get("offset", 0)
    resp = _tg_get_updates(offset + 1 if offset else None)
    updates = resp.get("result", [])
    if not updates:
        print("No new messages.")
        render_dashboard()
        return

    handled = 0
    for u in updates:
        offset = max(offset, u["update_id"])
        msg = u.get("message") or u.get("edited_message")
        if not msg or "text" not in msg:
            continue
        text = msg["text"].strip()
        chat_id = str(msg["chat"]["id"])
        # Only act on the configured chat (ignore strangers).
        if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
            print(f"Ignoring message from chat {chat_id}")
            continue
        print(f"> {text}")
        try:
            msgs = [_system_message()]
            reply = converse(msgs, text)
            if not _sent_during(msgs) and reply:
                send_telegram(reply)
                print(f"< {reply}")
        except Exception as e:
            print(f"Error handling message: {e}")
            send_telegram("Sorry, I hit an error handling that. Try again in a bit.")
        handled += 1

    _write_json(TG_OFFSET, {"offset": offset})
    render_dashboard()
    print(f"Handled {handled} message(s). Offset now {offset}.")


# ---------------------------------------------------------------------------
# Daily lunch suggestion
# ---------------------------------------------------------------------------
SUGGEST_PROMPT = (
    "It's late morning — suggest ONE lunch option near the office for me. "
    "Steps: (1) read my food log for the last 3 days; (2) check read_recent_picks so you "
    "know what you've already suggested; (3) search a FEW different cuisines/dish types "
    "(e.g. salad, japanese, malay, thai, poke, sandwich, yong tau foo, korean) — NOT just "
    "'healthy' — to build a varied set of nearby options. Then choose ONE spot + dish that is "
    "reasonably healthy, fits my remaining calories, and is clearly DIFFERENT from what I've "
    "eaten or been suggested recently — rotate the place AND the cuisine, don't repeat a "
    "recent pick. Deliver it by calling propose_pick (place, dish, calories, note) — do NOT "
    "use send_telegram — so I can accept it with one tap."
)


def _last_telegram_text(messages):
    """Pull the message text from the last send_telegram tool call, if any."""
    for m in reversed(messages):
        for tc in m.get("tool_calls") or []:
            if tc["function"]["name"] == "send_telegram":
                try:
                    return json.loads(tc["function"].get("arguments") or "{}").get("message", "")
                except json.JSONDecodeError:
                    return ""
    return ""


def suggest():
    # Feed recent picks straight into the prompt so variety doesn't depend on the
    # model remembering to call the tool.
    recent = load_suggestions()[-10:]
    avoid = "; ".join(s.get("pick", "")[:90] for s in recent) or "nothing yet"
    prompt = (
        SUGGEST_PROMPT
        + "\n\nRecently suggested (do NOT repeat these places/cuisines — pick something "
          "clearly different):\n" + avoid
    )
    messages = [_system_message()]
    reply = converse(messages, prompt)
    # propose_pick delivers the suggestion (buttoned message) and records it.
    # Fallback only if the model somehow didn't send anything.
    if not _sent_during(messages) and reply:
        send_telegram(reply)
    print("Suggestion run complete.")


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------
def tg_updates():
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set.")
        return
    resp = _tg_get_updates()
    for u in resp.get("result", []):
        m = u.get("message") or {}
        chat = m.get("chat", {})
        print(f"update_id={u['update_id']} chat_id={chat.get('id')} "
              f"name={chat.get('first_name','')} text={m.get('text','')!r}")


def run_once(prompt):
    reply = converse([_system_message()], prompt)
    print(reply or "(no text; model used tools only)")
    render_dashboard()


def repl():
    messages = [_system_message()]
    print("lunch-harness REPL. Ctrl-C to quit.")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        reply = converse(messages, text)
        print(f"bot> {reply or '(used tools only)'}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if not args:
        repl()
    elif args[0] == "--poll":
        poll()
    elif args[0] == "--suggest":
        suggest()
    elif args[0] == "--dashboard":
        print("Wrote", render_dashboard())
    elif args[0] == "--tg-updates":
        tg_updates()
    elif args[0] == "--models":
        list_models()
    elif args[0] == "--once":
        run_once(args[1] if len(args) > 1 else "Suggest me a healthy lunch near the office.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
