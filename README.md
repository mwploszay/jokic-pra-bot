# Jokić PRA Tracker

An X (Twitter) bot that tracks Nikola Jokić's **Points + Rebounds + Assists (PRA)** betting line for every Denver Nuggets game and posts whether he went over or under.

For each game it:

1. **Before tip-off** (about 5 minutes out), fetches Jokić's DraftKings PRA line and tweets it.
2. **After the game**, pulls his final box score, grades the result against the line it tweeted, and posts **OVER ✅ / UNDER ❌ / PUSH ➖** with the running season record. If he doesn't play, the line is marked void.

Example result tweet:

```
Final: Jokić 32 pts / 10 reb / 11 ast = 53 PRA
Line: 46.5 → OVER ✅

2026-27 season: 2 Over | 5 Under (29% Overs)
#Jokić #Jokic #Joker #JokićPRA #JokicPRA #JokerPRA #Nuggets
```

## Tech stack

- **Python 3** with [`requests`](https://pypi.org/project/requests/) and [`tweepy`](https://pypi.org/project/tweepy/)
- **[The Odds API](https://the-odds-api.com/) (v4)** for the Nuggets schedule and DraftKings odds, market `player_points_rebounds_assists`
- **ESPN's public NBA feeds** (`scoreboard` and `summary`) for game matching and final box scores. These feeds are undocumented and could change without notice.
- **X API v2 via tweepy**, using OAuth 1.0a user-context authentication

## How it works

The bot runs as a long-lived loop that checks every 30 seconds. It refreshes the schedule hourly, or every few minutes when a game is close, so delays and tip-time changes are picked up. It saves its progress to `state.json`, which holds tracked games, the line it tweeted, results and the season record. That lets it restart at any time without double-posting or double-counting a result. If a tweet fails, it retries the post alone; the grading isn't redone.

## Configuration

All settings come from environment variables. No keys are stored in the code.

| Variable | Purpose |
|---|---|
| `ODDS_API_KEY` | The Odds API key (required) |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | X app OAuth 1.0a credentials (only needed for live posting) |
| `DRY_RUN` | `1` (the default) prints tweets instead of posting them. `0` posts for real. |
| `STATE_PATH` | Where to save progress (default `./state.json`) |

## Running in dry-run mode

Dry-run is the default: tweets are printed to the console and nothing is posted. Only an Odds API key is required.

```bash
pip install -r requirements.txt
```

macOS / Linux:

```bash
export ODDS_API_KEY="your-odds-api-key"
python jokic_pra_bot.py
```

Windows PowerShell:

```powershell
$env:ODDS_API_KEY = "your-odds-api-key"
python jokic_pra_bot.py
```

The first log line confirms the mode: `Starting. DRY_RUN=True`. Leave it running through a Nuggets game to see the pre-game and result tweets. Stop it with Ctrl+C.

To post for real, also set the four X variables and `DRY_RUN=0`. The X app needs **Read and write** permission.

## Deployment

In production it runs 24/7 on a **DigitalOcean Droplet** (Ubuntu 24.04) as a **systemd service**:

- The code and a Python virtual environment live in `/opt/jokic-pra-bot`, owned by a dedicated low-privilege `jokic` user.
- Credentials are loaded from a root-only environment file (`/etc/jokic-pra-bot.env`, permissions `600`) through systemd's `EnvironmentFile`.
- `Restart=always` with a 30-second delay restarts the bot after a crash. The service is enabled, so it also starts on boot.
- Logs are kept by journald: `journalctl -u jokic-pra-bot`.

The unit file is in [`deploy/jokic-pra-bot.service`](deploy/jokic-pra-bot.service).

## Not in this repo

- **API keys and tokens.** Supply them as environment variables.
- **`state.json`.** It's created at runtime and holds the live season record.
