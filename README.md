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
  from the current state on every request, so it's always up to date. Also mirrored to
  **GitHub Pages** (`docs/index.html`) as a static, slightly-delayed secondary view.

**Instant replies via a Telegram webhook.** A small Vercel Python function
(`api/telegram.py`) receives each message the moment you send it and reuses `harness.py`
for the agent. **State stays in GitHub** (`data/*.json`) and the **dashboard stays on GitHub
Pages** — the function commits new meals to the repo, which triggers a GitHub Action to
re-render the dashboard.

## How it works

```
you ──Telegram webhook──▶ Vercel fn (api/telegram.py) ──▶ harness.converse() (OpenCode model)
                              │  instant reply           │ tools: log_meal / update / delete
                              ▼                          ▼
             commit data/food_log.json to GitHub  (reads state from GitHub, works in /tmp)
                              │
                              ▼  (push to data/**)
              GitHub Action dashboard.yml ──▶ harness.py --dashboard ──▶ Pages

cron Mon-Fri 11:50 SGT ──▶ lunch-suggest.yml ──▶ harness.py --suggest
                       read_food_log → search_places → pick → send_telegram
```

The daily lunch suggestion stays on **GitHub Actions cron** (it doesn't need to be
real-time). Only the interactive logging moved to the webhook.

## Files

| Path | What |
|------|------|
| `harness.py` | The whole agent: config, model call, tool loop, tools, dashboard renderer, CLI. |
| `api/telegram.py` | Vercel webhook: receives Telegram messages, runs the agent, commits state to GitHub. |
| `system_prompt.md` | Standing orders (role, auto-log+correct contract, health nudges). |
| `pyproject.toml` | Vercel Python entrypoint config (`api.telegram:handler`). |
| `data/food_log.json` | Meal history — the "database". |
| `data/suggestions.json` | Daily lunch picks (feeds the Pages log). |
| `docs/index.html` | Generated GitHub Pages dashboard. |
| `.github/workflows/dashboard.yml` | On push to `data/**`: re-render the dashboard for Pages. |
| `.github/workflows/lunch-suggest.yml` | Weekday (Mon–Fri) 11:50 SGT lunch suggestion. |

## Setup

1. **Clone / push** this repo to GitHub.
2. **Telegram bot:** message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
   Message your new bot once, then run `python3 harness.py --tg-updates` locally to get your
   `chat_id`.
3. **OpenCode Go:** get your API key + model id (OpenAI-compatible endpoint).
4. **Google Places:** use your existing key (Places API **New/v1** enabled, billing on).
5. **Local `.env`:** `cp .env.example .env` and fill in the five values.
6. **GitHub secrets** (Settings → Secrets and variables → Actions): add
   `OPENCODE_API_KEY`, `OPENCODE_MODEL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   `GOOGLE_PLACES_API_KEY` (used by the daily lunch-suggest + dashboard workflows).
7. **GitHub Pages:** Settings → Pages → *Deploy from a branch* → `main` / `/docs`.
8. **Deploy the webhook to Vercel** (see below).

### Deploy the webhook (Vercel)

1. **GitHub PAT:** create a *fine-grained* token with **Contents: Read and write** on this
   repo only. This lets the function commit logged meals.
2. **Deploy:** `vercel` then `vercel --prod` (or connect the repo in the Vercel dashboard).
3. **Env vars** (`vercel env add …` or dashboard) — the same model/Telegram/Places keys **plus**
   `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_BRANCH`, `TELEGRAM_WEBHOOK_SECRET` (any random string),
   and `LUNCH_DATA_DIR=/tmp/lh/data`, `LUNCH_DOCS_DIR=/tmp/lh/docs`.
4. **Register the webhook** with Telegram (replace the URL + token):
   ```bash
   curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
     -d "url=https://<your-app>.vercel.app/api/telegram" \
     -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
   ```
   Setting a webhook disables `getUpdates` polling — that's expected.

> ⚠️ GitHub cron (the daily suggestion) pauses after ~60 days of repo inactivity; any push or
> manual run re-arms it. The webhook itself is always-on and unaffected.

## Local usage

```bash
python3 harness.py                 # interactive REPL (debug)
python3 harness.py --once "..."    # one-shot prompt
python3 harness.py --suggest       # run the daily suggestion
python3 harness.py --dashboard     # re-render docs/index.html from data
python3 harness.py --tg-updates    # find your Telegram chat id (only works before a webhook is set)
python3 harness.py --models        # list available models
```

## Config

Env vars (via real env, `.env`, or — for `OPENCODE_API_KEY` — the macOS Keychain generic
password `opencode-api-key`):

`OPENCODE_API_KEY`, `OPENCODE_MODEL`, `OPENCODE_BASE_URL`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `GOOGLE_PLACES_API_KEY`, `DAILY_CALORIE_TARGET` (default 2100).

The office location and search radius are constants at the top of `harness.py`
(`OFFICE_LAT`, `OFFICE_LNG`, `SEARCH_RADIUS_M`).

## Notes & caveats

- Calorie numbers are **LLM estimates** — a tracking aid, not medical/nutrition advice.
- Google Places returns names/ratings/price, **not** calories; the model estimates those
  from the dish.
- Stdlib only; no `pip install`. Python 3.12 in CI.
