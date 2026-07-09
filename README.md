# 🍱 lunch-harness

A one-file personal **lunch suggester + calorie tracker** for the office at **1 Depot Rd,
Singapore**. Pivoted from [hadr-harness](https://github.com/Weiming95/hadr-harness) — same
philosophy (*the harness is the loop, the tools, and the interface; the model is just a
text-in/text-out function*), different job.

- **Log meals over Telegram.** Text the bot what you ate ("chicken rice and kopi"). It
  estimates the calories, logs it immediately, and tells you the running total. Reply to
  correct it ("make it 700", "remove that").
- **Log any meal, any time.** Breakfast, lunch, dinner, or a snack — text it and it's logged
  toward your daily total. Give your own calorie count and it uses that; otherwise it estimates.
- **Weekday lunch suggestion.** Each weekday (Mon–Fri, 11:50 SGT) it looks at your last few
  days of meals, searches eateries near the office (Google Places), and suggests one option.
- **Live dashboard on Vercel** (`GET /`) — a calorie dashboard + recent lunch picks, rendered
  from the current state on every request, so it's always up to date.

**Everything runs on Vercel; data lives in Upstash Redis.** A single Vercel Python function
(`api/telegram.py`) is the Telegram webhook, the dashboard, and the daily-suggestion cron
target — all reusing `harness.py` for the agent. State (food log + suggestions) is stored in
**Upstash Redis**, so nothing is committed to git on every meal. GitHub holds only the code.

## How it works

```
you ──Telegram webhook (POST /api/telegram)──▶ Vercel fn ──▶ harness.converse() (OpenCode)
                                                   │ tools: log_meal / update / delete / search
                                                   ▼
                                        read/write food log in Upstash Redis

Vercel Cron (Mon-Fri 11:50 SGT) ──▶ GET /api/suggest ──▶ harness.suggest()
                       read_food_log → read_recent_picks → search_places → pick → Telegram

anyone ──▶ GET / ──▶ dashboard rendered live from Redis
```

No GitHub Actions, no per-meal commits, no static Pages build to wait on — the dashboard is
always current because it reads Redis on each request.

## Files

| Path | What |
|------|------|
| `harness.py` | The whole agent: config, model call, tool loop, tools, store (Redis/file), dashboard, CLI. |
| `api/telegram.py` | Vercel function: webhook (POST), daily-suggest cron (`/api/suggest`), and live dashboard (`GET /`). |
| `system_prompt.md` | Standing orders (role, auto-log+correct contract, health + variety nudges). |
| `pyproject.toml` | Vercel Python entrypoint config (`api.telegram:handler`). |
| `vercel.json` | Vercel Cron schedule for the daily suggestion. |

State (food log + suggestions) is **not** in the repo — it lives in Upstash Redis under the
keys `food_log` and `suggestions`. Locally (no Redis env) it falls back to `data/*.json`.

## Setup

1. **Push** this repo to GitHub (code only — no data or secrets).
2. **Telegram bot:** message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
   Message it once, then `python3 harness.py --tg-updates` locally to get your `chat_id`.
3. **OpenCode Go:** API key + model id. **Google Places:** key with Places API **New/v1** on.
4. **Upstash Redis:** in the Vercel dashboard → project → *Storage* → add **Upstash Redis**
   (Marketplace, free tier). It auto-adds `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`.
5. **Deploy to Vercel:** `vercel --prod` (from the repo).
6. **Env vars** (`vercel env add … production` or dashboard): `OPENCODE_API_KEY`,
   `OPENCODE_MODEL`, `OPENCODE_BASE_URL`, `GOOGLE_PLACES_API_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`, `TELEGRAM_WEBHOOK_SECRET` (random), `CRON_SECRET` (random),
   `DAILY_CALORIE_TARGET`. (Upstash vars come from step 4.)
7. **Register the webhook** with Telegram (replace URL + token):
   ```bash
   curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
     -d "url=https://<your-app>.vercel.app/api/telegram" \
     -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
   ```
   Setting a webhook disables `getUpdates` polling — expected.

> The daily suggestion runs via **Vercel Cron** (`/api/suggest`, Mon–Fri 11:50 SGT). On the
> Hobby plan cron timing isn't minute-precise, so the function also guards to weekdays itself.

## Local usage

```bash
python3 harness.py                 # interactive REPL (debug)
python3 harness.py --once "..."    # one-shot prompt
python3 harness.py --suggest       # run the daily suggestion
python3 harness.py --dashboard     # render docs/index.html from local data (dev only)
python3 harness.py --tg-updates    # find your Telegram chat id (only before a webhook is set)
python3 harness.py --models        # list available models
```

## Config

Env vars (via real env, `.env`, or — for `OPENCODE_API_KEY` — the macOS Keychain generic
password `opencode-api-key`):

`OPENCODE_API_KEY`, `OPENCODE_MODEL`, `OPENCODE_BASE_URL`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `TELEGRAM_WEBHOOK_SECRET`, `GOOGLE_PLACES_API_KEY`, `CRON_SECRET`,
`DAILY_CALORIE_TARGET` (default 2100), and `UPSTASH_REDIS_REST_URL` /
`UPSTASH_REDIS_REST_TOKEN` (store; falls back to local files if unset).

The office location and search radius are constants at the top of `harness.py`
(`OFFICE_LAT`, `OFFICE_LNG`, `SEARCH_RADIUS_M`).

## Notes & caveats

- Calorie numbers are **LLM estimates** — a tracking aid, not medical/nutrition advice.
- Google Places returns names/ratings/price, **not** calories; the model estimates those
  from the dish.
- Stdlib only; no `pip install`. Python 3.12 in CI.
