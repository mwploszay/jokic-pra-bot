#!/usr/bin/env python3
"""
Jokic PRA Tracker
-----------------
Long-running bot that, for every Denver Nuggets game:
  1. Tweets Jokic's DraftKings Points+Rebounds+Assists line ~5 min before tip.
  2. Polls ESPN's box score after the game and tweets OVER / UNDER / PUSH
     vs. the line from that pre-game tweet, plus the season-long running record.

Environment variables
  ODDS_API_KEY           The Odds API key (the-odds-api.com)
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET   X app OAuth 1.0a creds
  DRY_RUN                "1" (default) logs tweets instead of posting. Set "0" to go live.
  STATE_PATH             Where to persist state (default ./state.json)
"""
import json
import logging
import os
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------- config ---
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
STATE_PATH = Path(os.environ.get("STATE_PATH", "state.json"))

BOOK = "draftkings"
MARKET = "player_points_rebounds_assists"   # Odds API market key for PRA
PLAYER_NAME = "nikola jokic"                # compared after accent-stripping
JOKIC_ESPN_ID = "3112335"                   # ESPN athlete id
TEAM_NAME = "Denver Nuggets"
TEAM_ABBR = "DEN"
HASHTAGS = "#Jokić #Jokic #Joker #JokićPRA #JokicPRA #JokerPRA #Nuggets"   # last line of every tweet

ODDS_BASE = "https://api.the-odds-api.com/v4"
# ESPN's public (undocumented) feeds; no key needed, but they can change without notice.
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"

TICK_SECONDS = 30
PRE_TWEET_MINUTES = 5          # tweet the line this many minutes before tip
POLL_AFTER_TIP_MINUTES = 100   # start polling the box score this long after tip
GIVE_UP_HOURS = 6              # abandon a game this long after tip
SOON_MINUTES = 90              # a game tipping within this window counts as "soon"
SOON_REFRESH_MINUTES = 3       # schedule refresh interval while a game is soon
ID_LOOKUP_MINUTES = 120        # start matching the ESPN game id this long before tip

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jokic-pra")


# ------------------------------------------------------------------ utils ---
def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def season_label(dt):
    start = dt.year if dt.month >= 8 else dt.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def fmt_price(p):
    return "n/a" if p is None else f"{int(p):+d}"


def due(game, key, seconds):
    """Simple per-game rate limiter."""
    last = game.get(key)
    if last and (now_utc() - parse_ts(last)).total_seconds() < seconds:
        return False
    game[key] = now_utc().isoformat()
    return True


# ------------------------------------------------------------------ state ---
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"games": {}, "records": {}, "last_schedule": None}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def record_for(state, season):
    return state["records"].setdefault(season, {"over": 0, "under": 0, "push": 0, "void": 0})


# ------------------------------------------------------------------ X/tweet --
def tweet(text):
    log.info("TWEET (%s):\n%s", "dry-run" if DRY_RUN else "live", text)
    if DRY_RUN:
        return True
    import tweepy
    # Fresh client per tweet: tweets are hours apart, and X drops idle
    # connections, so a reused one fails on the first post-game attempt.
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )
    try:
        client.create_tweet(text=text)
    finally:
        client.session.close()
    return True


# ------------------------------------------------------------- data sources --
def get_nuggets_events():
    """Upcoming/live NBA events (this endpoint does not use quota credits)."""
    r = requests.get(
        f"{ODDS_BASE}/sports/basketball_nba/events",
        params={"apiKey": ODDS_API_KEY, "dateFormat": "iso"},
        timeout=20,
    )
    r.raise_for_status()
    return [e for e in r.json() if TEAM_NAME in (e["home_team"], e["away_team"])]


def get_line(event_id):
    """Return {'line', 'over', 'under'} for Jokic PRA at DraftKings, or None."""
    r = requests.get(
        f"{ODDS_BASE}/sports/basketball_nba/events/{event_id}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "bookmakers": BOOK,
            "markets": MARKET,
            "oddsFormat": "american",
        },
        timeout=20,
    )
    r.raise_for_status()
    for bk in r.json().get("bookmakers", []):
        if bk["key"] != BOOK:
            continue
        for m in bk.get("markets", []):
            if m["key"] != MARKET:
                continue
            line = over = under = None
            for o in m.get("outcomes", []):
                if norm(o.get("description", "")) != PLAYER_NAME:
                    continue
                if o["name"] == "Over":
                    line, over = o.get("point"), o.get("price")
                elif o["name"] == "Under":
                    line = line if line is not None else o.get("point")
                    under = o.get("price")
            if line is not None:
                return {"line": line, "over": over, "under": under}
    return None


def find_espn_event_id(tip):
    """Match our event to an ESPN game id by team + tip time.

    ESPN files games under the US date, which can be the UTC date or the day
    before, so check both. Past dates stay available, so late games are fine.
    """
    for day in (tip - timedelta(days=1), tip):
        r = requests.get(ESPN_SCOREBOARD, params={"dates": day.strftime("%Y%m%d")}, timeout=20)
        r.raise_for_status()
        for ev in r.json().get("events", []):
            teams = [c["team"]["abbreviation"] for c in ev["competitions"][0]["competitors"]]
            if TEAM_ABBR in teams and abs((parse_ts(ev["date"]) - tip).total_seconds()) < 3 * 3600:
                return ev["id"]
    return None


def get_jokic_box(event_id):
    """Return (is_final, stats|None). stats None == DNP."""
    r = requests.get(ESPN_SUMMARY, params={"event": event_id}, timeout=20)
    r.raise_for_status()
    data = r.json()
    final = bool(data["header"]["competitions"][0]["status"]["type"]["completed"])
    for team in data.get("boxscore", {}).get("players", []):
        for block in team.get("statistics", []):
            keys = block.get("keys", [])
            for a in block.get("athletes", []):
                ath = a.get("athlete", {})
                if str(ath.get("id")) != JOKIC_ESPN_ID and norm(ath.get("displayName", "")) != PLAYER_NAME:
                    continue
                stats = dict(zip(keys, a.get("stats", [])))
                mins = stats.get("minutes", "")
                if a.get("didNotPlay") or not mins.isdigit() or int(mins) == 0:
                    return final, None
                return final, {
                    "pts": int(stats["points"]),
                    "reb": int(stats["rebounds"]),
                    "ast": int(stats["assists"]),
                }
    return final, None


# ----------------------------------------------------------------- tweets ---
def pre_game_text(game, ln):
    return (
        f"\U0001F0CF Jokić PRA line tonight: {ln['line']}\n"
        f"DraftKings: Over {fmt_price(ln['over'])} / Under {fmt_price(ln['under'])}\n"
        f"{game['matchup']} — tip in {PRE_TWEET_MINUTES} min.\n"
        f"{HASHTAGS}"
    )


def record_line(rec):
    s = f"{rec['over']} Over | {rec['under']} Under"
    if rec["push"]:
        s += f" | {rec['push']} Push"
    if rec.get("void"):
        s += f" | {rec['void']} Void"
    graded = rec["over"] + rec["under"]
    if graded:
        s += f" ({rec['over'] / graded:.0%} Overs)"
    return s


def post_game_text(game, stats, result, rec, season):
    pra = stats["pts"] + stats["reb"] + stats["ast"]
    icon = {"OVER": "\u2705", "UNDER": "\u274C", "PUSH": "\u2796"}[result]
    return (
        f"Final: Jokić {stats['pts']} pts / {stats['reb']} reb / {stats['ast']} ast "
        f"= {pra} PRA\n"
        f"Line: {game['posted']['line']} \u2192 {result} {icon}\n\n"
        f"{season} season: {record_line(rec)}\n"
        f"{HASHTAGS}"
    )


# ------------------------------------------------------------- game engine ---
def refresh_schedule(state):
    # Check hourly, but every few minutes when a game is close so a late delay
    # is caught before the pre-game tweet goes out.
    now = now_utc()
    tip_soon = any(
        not g["done"] and timedelta(0) < parse_ts(g["tip"]) - now <= timedelta(minutes=SOON_MINUTES)
        for g in state["games"].values()
    )
    interval = timedelta(minutes=SOON_REFRESH_MINUTES if tip_soon else 60)
    last = state.get("last_schedule")
    if last and (now - parse_ts(last)) < interval:
        return
    state["last_schedule"] = now.isoformat()
    for ev in get_nuggets_events():
        game = state["games"].get(ev["id"])
        if game:
            if not game["done"] and game["tip"] != ev["commence_time"]:
                log.info("Tip time changed for %s: %s -> %s", game["matchup"], game["tip"], ev["commence_time"])
                game["tip"] = ev["commence_time"]
                game.pop("espn_event_id", None)  # re-match against the new time
            continue
        home = ev["home_team"] == TEAM_NAME
        opp = ev["away_team"] if home else ev["home_team"]
        state["games"][ev["id"]] = {
            "event_id": ev["id"],
            "tip": ev["commence_time"],
            "matchup": f"Nuggets vs {opp}" if home else f"Nuggets @ {opp}",
            "done": False,
        }
        log.info("Tracking new game: %s at %s", state["games"][ev["id"]]["matchup"], ev["commence_time"])


def process_game(state, game):
    now = now_utc()
    tip = parse_ts(game["tip"])
    until_tip = tip - now

    if now > tip + timedelta(hours=GIVE_UP_HOURS):
        log.warning("Giving up on %s", game["event_id"])
        game["done"] = True
        return

    # 0) Match the ESPN game id ahead of time
    if (not game.get("espn_event_id") and until_tip <= timedelta(minutes=ID_LOOKUP_MINUTES)
            and due(game, "last_id_try", 300)):
        game["espn_event_id"] = find_espn_event_id(tip)

    # 1) Pre-game tweet at T-5 min (retry once a minute until tip if no line yet)
    if (not game.get("pre_posted") and timedelta(0) < until_tip <= timedelta(minutes=PRE_TWEET_MINUTES)
            and due(game, "last_pre_try", 60)):
        ln = get_line(game["event_id"])
        if ln:
            if tweet(pre_game_text(game, ln)):
                game["posted"] = ln  # graded against exactly what we tweeted
                game["pre_posted"] = True
        else:
            log.info("No DraftKings PRA line for Jokic yet")

    # 2) Post-game grading
    if now >= tip + timedelta(minutes=POLL_AFTER_TIP_MINUTES) and due(game, "last_box_try", 60):
        if not game.get("posted"):
            log.warning("No pre-game line was tweeted for %s; skipping.", game["event_id"])
            game["done"] = True
            return
        if not game.get("espn_event_id"):
            game["espn_event_id"] = find_espn_event_id(tip)
            if not game["espn_event_id"]:
                return
        season = season_label(tip)
        rec = record_for(state, season)

        # Grade once. The result and the record bump are stored together, so if
        # the tweet below fails we retry only the tweet, never the count.
        if not game.get("result"):
            final, stats = get_jokic_box(game["espn_event_id"])
            if not final:
                return  # not final yet
            if stats is None:
                result = "VOID"
            else:
                pra = stats["pts"] + stats["reb"] + stats["ast"]
                line = game["posted"]["line"]
                result = "OVER" if pra > line else "UNDER" if pra < line else "PUSH"
            rec[result.lower()] += 1
            game["result"] = result
            game["stats"] = stats
            save_state(state)

        if game["result"] == "VOID":
            text = (f"Jokić did not play, so tonight's PRA line ({game['posted']['line']}) is void.\n\n"
                    f"{season} season: {record_line(rec)}\n{HASHTAGS}")
        else:
            text = post_game_text(game, game["stats"], game["result"], rec, season)
        if tweet(text):
            game["done"] = True


def main():
    if not ODDS_API_KEY:
        raise SystemExit("Set ODDS_API_KEY")
    log.info("Starting. DRY_RUN=%s", DRY_RUN)
    state = load_state()
    while True:
        try:
            refresh_schedule(state)
        except Exception:
            log.exception("Schedule refresh failed; will retry")
        # Each game gets its own try so one bad request can't stall the others.
        for game in state["games"].values():
            if game["done"]:
                continue
            try:
                process_game(state, game)
            except Exception:
                log.exception("Processing %s failed; will retry", game["event_id"])
        try:
            save_state(state)
        except Exception:
            log.exception("Saving state failed; will retry")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
