import datetime
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(line_buffering=True)

# --- CONFIG ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8435489741")

EVENTS_URL = (
    "https://api.matchbook.com/edge/rest/events"
    "?sport-ids=24735152712200&per-page=100&states=open,suspended"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

FAST_LOOP = 5                 # tick when races are in/near the window
IDLE_LOOP = 30                # tick when nothing is near the window
STEAM_WINDOW_START = 1800     # T-30min: window opens, baseline set
STEAM_WINDOW_END = 60         # T-1min: window closes
STEAM_DROP_PCT = 0.20         # alert when drop is MORE than 20%
SEEN_TTL_SECONDS = 86400
STATE_FILE = os.environ.get("STATE_FILE", "steam_state.json")
UK_TZ = ZoneInfo("Europe/London")

steam_baseline = {}   # runner_id -> {"mid","ts","name","race","race_time","race_epoch"}
steam_alerted = {}    # runner_id -> epoch


def log(msg):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def err(msg, exc=None):
    log(f"[ERR] {msg}")
    if exc is not None:
        print(traceback.format_exc(), flush=True)


def is_today_uk(event_dt_utc):
    now_uk = datetime.datetime.now(UK_TZ)
    return event_dt_utc.astimezone(UK_TZ).date() == now_uk.date()


# ---------- persistence ----------
def load_state():
    if not os.path.exists(STATE_FILE):
        log("No state file — cold start.")
        return
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        now = time.time()
        raw = d.get("alerted")
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    if now - float(v) < SEEN_TTL_SECONDS:
                        steam_alerted[int(k)] = float(v)
                except Exception:
                    continue
        log(f"Loaded {len(steam_alerted)} previously alerted.")
    except Exception as e:
        err(f"State load failed: {e}", e)


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"alerted": steam_alerted}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        err(f"State save failed: {e}", e)


def purge_stale():
    now = time.time()
    for r in [r for r, v in steam_baseline.items()
              if now > v.get("race_epoch", now) + 600]:
        steam_baseline.pop(r, None)
    for r in [r for r, t in steam_alerted.items()
              if now - t > SEEN_TTL_SECONDS]:
        steam_alerted.pop(r, None)


# ---------- network ----------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    ).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True
                err(f"Telegram HTTP {resp.status}")
        except Exception as e:
            err(f"Telegram error ({attempt + 1}/3): {e}")
            time.sleep(2)
    return False


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- parsing ----------
def is_withdrawn(runner):
    status = str(runner.get("status", "")).lower()
    wd = runner.get("withdrawn")
    if wd is True or (isinstance(wd, str) and wd.lower() == "true"):
        return True
    return status in ("withdrawn", "scratched", "removed", "non-runner", "nonrunner")


def extract_book(runner):
    """Binary exchange: side 'win' = back, side 'lose' -> lay-equivalent."""
    win_prices, lose_prices = [], []
    for p in runner.get("prices", []) or []:
        if not isinstance(p, dict):
            continue
        try:
            dec = float(p.get("decimal-odds") or p.get("odds"))
        except (TypeError, ValueError):
            continue
        if not dec or dec <= 1.0:
            continue
        side = str(p.get("side", "")).lower()
        if side in ("win", "back"):
            win_prices.append(dec)
        elif side in ("lose", "lay"):
            lose_prices.append(dec)

    best_back = max(win_prices) if win_prices else None
    best_lay = None
    if lose_prices:
        bl = max(lose_prices)
        if bl > 1.0:
            best_lay = bl / (bl - 1.0)
    mid = ((best_back + best_lay) / 2 if (best_back and best_lay)
           else (best_back or best_lay))
    try:
        vol = float(runner.get("volume") or 0)
    except (TypeError, ValueError):
        vol = 0.0
    return mid, vol


def build_steam_alert(name, race, race_time, baseline, base_ts,
                      current, vol, mins_to_off):
    drop = (baseline - current) / baseline * 100
    return (
        f"🔥 *MARKET MOVE — HEAVY BACKING*\n\n"
        f"🏇 *Horse:* {name}\n"
        f"📍 *Race:* {race}\n"
        f"📉 *Odds:* `{baseline:.2f}` ➜ `{current:.2f}`\n"
        f"⚡ *Drop:* `-{drop:.1f}%` since `{base_ts} UTC`\n"
        f"💰 *Matched Volume:* `{vol:,.0f}`\n"
        f"⏳ *Off in:* ~{mins_to_off} min\n"
        f"⏰ *Race Time:* {race_time} UTC"
    )


# ---------- core ----------
def scan(warmup=False):
    """Returns (alerts_sent, seconds_to_next_window_race)."""
    steams = races_in_window = runners_tracked = 0
    next_window_in = None

    try:
        events = get_json(EVENTS_URL).get("events", []) or []
    except Exception as e:
        err(f"Events fetch failed: {e}")
        return 0, None

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_epoch = time.time()

    for event in events:
        start_str = event.get("start")
        if not start_str:
            continue
        try:
            event_dt = datetime.datetime.fromisoformat(
                start_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        delta = (event_dt - now_utc).total_seconds()
        if delta <= 0 or not is_today_uk(event_dt):
            continue

        # track when the next race will enter the window (for adaptive sleep)
        if delta > STEAM_WINDOW_START:
            gap = delta - STEAM_WINDOW_START
            if next_window_in is None or gap < next_window_in:
                next_window_in = gap
            continue

        if delta < STEAM_WINDOW_END:
            continue

        # --- race is inside T-30m .. T-1m ---
        races_in_window += 1
        event_id = event.get("id")
        event_name = event.get("name", "Unknown Race")
        race_time = start_str[:16].replace("T", " ")
        race_epoch = event_dt.timestamp()
        mins_to_off = int(delta / 60)

        for market in event.get("markets", []) or []:
            if "win" not in str(market.get("name", "")).lower():
                continue

            market_id = market.get("id")
            url = (
                f"https://api.matchbook.com/edge/rest/events/{event_id}"
                f"/markets/{market_id}/runners"
                "?include-withdrawn=true&include-prices=true&price-depth=3"
            )

            try:
                runners = get_json(url).get("runners", []) or []
            except Exception as e:
                err(f"Market {market_id} fetch failed: {e}")
                continue

            for runner in runners:
                rid = runner.get("id")
                if not rid or is_withdrawn(runner):
                    continue
                mid, vol = extract_book(runner)
                if not mid:
                    continue
                runners_tracked += 1
                rname = runner.get("name", "Unknown")

                base = steam_baseline.get(rid)
                if base is None:
                    steam_baseline[rid] = {
                        "mid": mid,
                        "ts": now_utc.strftime("%H:%M:%S"),
                        "name": rname,
                        "race": event_name,
                        "race_time": race_time,
                        "race_epoch": race_epoch,
                    }
                    continue

                if rid in steam_alerted:
                    continue
                if vol <= 0:
                    continue
                # strict: drop must be MORE than 20%
                if mid >= base["mid"] * (1 - STEAM_DROP_PCT):
                    continue

                if warmup:
                    steam_alerted[rid] = now_epoch
                    continue

                msg = build_steam_alert(
                    rname, event_name, race_time,
                    base["mid"], base["ts"], mid, vol, mins_to_off)
                log(f"STEAMER: {rname} @ {event_name} "
                    f"{base['mid']:.2f}->{mid:.2f} vol={vol:.0f} "
                    f"T-{mins_to_off}m")
                if send_telegram(msg):
                    steam_alerted[rid] = now_epoch
                    steams += 1
                    save_state()

    purge_stale()
    log(f"in_window_races={races_in_window} tracked={runners_tracked} "
        f"baselines={len(steam_baseline)} alerts={steams}")
    return steams, next_window_in if races_in_window == 0 else 0


if __name__ == "__main__":
    log("=== STEAMER MONITOR (Runner 2) STARTING ===")
    log(f"Window: T-{STEAM_WINDOW_START // 60}m to T-{STEAM_WINDOW_END // 60}m "
        f"| drop > {STEAM_DROP_PCT:.0%} from window-open price "
        f"| volume > 0 | today's UK card | no odds cap")
    load_state()
    log("Warm-up scan (no alerts)...")
    try:
        scan(warmup=True)
    except Exception as e:
        err(f"Warm-up failed: {e}", e)
    save_state()
    log("Alerting live.")

    cycle = 0
    while True:
        try:
            _, next_in = scan(warmup=False)
            cycle += 1
            if cycle % 60 == 0:
                save_state()
            # adaptive sleep: fast when racing is in/near the window,
            # relaxed when the next race is far away
            if next_in is not None and next_in > 300:
                sleep_for = IDLE_LOOP
            else:
                sleep_for = FAST_LOOP
        except KeyboardInterrupt:
            save_state()
            log("Stopped.")
            break
        except Exception as e:
            err(f"Loop error: {e}", e)
            sleep_for = FAST_LOOP
        time.sleep(sleep_for)
