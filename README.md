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
- **Published to GitHub Pages.** A daily calorie dashboard + recent lunch picks, rendered to
  `docs/index.html`.

No server. **GitHub Actions is the whole backend** — state lives in `data/*.json` and is
committed back to the repo on every run (which doubles as the Pages source).

## How it works

```
you ──Telegram──▶ poll.yml (every ~15m) ──▶ harness.py --poll ──▶ OpenCode model
                                                   │ tools: log_meal / update / delete
                                                   ▼
                                             data/food_log.json ──▶ render docs/index.html
                                                   │
                                          git commit + push (state + Pages)

cron Mon-Fri 11:50 SGT ──▶ lunch-suggest.yml ──▶ harness.py --suggest
                       read_food_log → search_places → pick → send_telegram
```

## Files

| Path | What |
|------|------|
| `harness.py` | The whole agent: config, model call, tool loop, tools, dashboard renderer, CLI. |
| `system_prompt.md` | Standing orders (role, auto-log+correct contract, health nudges). |
| `data/food_log.json` | Meal history — the "database". |
| `data/suggestions.json` | Daily lunch picks (feeds the Pages log). |
| `data/tg_offset.json` | Last processed Telegram `update_id`. |
| `docs/index.html` | Generated GitHub Pages dashboard. |
| `.github/workflows/poll.yml` | Every ~15 min: drain Telegram, log, rebuild, commit. |
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
   `GOOGLE_PLACES_API_KEY`.
7. **GitHub Pages:** Settings → Pages → *Deploy from a branch* → `main` / `/docs`.
8. **Kick it off:** run each workflow once from the Actions tab (`workflow_dispatch`).

> ⚠️ GitHub cron pauses after ~60 days of repo inactivity; any push or manual run re-arms it.
> Replies aren't instant — the poller runs every ~15 min.

## Local usage

```bash
python3 harness.py                 # interactive REPL (debug)
python3 harness.py --once "..."    # one-shot prompt
python3 harness.py --poll          # process Telegram messages once
python3 harness.py --suggest       # run the daily suggestion
python3 harness.py --dashboard     # re-render docs/index.html from data
python3 harness.py --tg-updates    # find your Telegram chat id
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
