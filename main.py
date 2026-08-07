import datetime
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# --- CONFIG ---
SPORTBEX_API_KEY = os.environ.get("SPORTBEX_API_KEY", "BVDAsHTYEWTRFzKRAJIzdHe117XQJXZPUOni7OqM")
SPORTBEX_BASE = os.environ.get(
    "SPORTBEX_BASE", "https://trial-api.sportbex.com/api")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8435489741")

SPORT_ID = 7

# --- ALERT CONDITIONS ---
# 1. runner status WAS "ACTIVE" and is now anything else
# 2. last fetched FIRST back price < MAX_ODDS
MAX_ODDS = float(os.environ.get("MAX_ODDS", "6.0"))

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))
WORKERS = int(os.environ.get("WORKERS", "12"))
TICK = 5
DISCOVER_SECONDS = 600
EVENT_REFETCH_SECONDS = 1800
STOP_POLL_AFTER_OFF = 300
PURGE_AFTER_OFF = 900
MAX_DAYS_AHEAD = float(os.environ.get("MAX_DAYS_AHEAD", "0"))  # 0 = no limit
SEEN_TTL_SECONDS = 259200
STATE_FILE = os.environ.get("STATE_FILE", "nr_sportbex_state.json")
UK_TZ = ZoneInfo("Europe/London")

# Event names that are forecast / reverse-forecast side events, not racing
BAD_EVENT_MARKERS = ("(rfc)", "(f/c)", "(fc)", "(tri)", "(tricast)",
                     "antepost", "ante-post", "ante post")

# marketName keywords that mean "not the win market"
NON_WIN_KEYWORDS = (
    "to be placed", "place", "forecast", "tricast", "match bet",
    "without", "winning distance", "number of", "insurance",
    "each way", "reverse", "double", "treble", "jockey", "trainer",
    "winner of", "favourite", "unnamed", "starting price", "outsider",
    "margin", "distance betting", "hi/lo", "under/over",
)

# market-level statuses we should not read runners from
DEAD_MARKET_STATUSES = {"CLOSED", "SETTLED", "VOIDED", "CANCELLED"}

registry = {}        # marketId -> race info + runner names
runner_state = {}    # "marketId:selectionId" -> last iteration snapshot
alerted = {}         # "marketId:selectionId" -> epoch
event_fetched = {}   # eventId -> epoch of last market-all-list fetch
_dumps_done = set()
_skipped_names = set()
_statuses_seen = {}
_mkt_statuses_seen = {}
_rate_limited_until = 0.0
_reg_counter = 0
API_STATS = {"calls": 0, "errors": 0}


def log(msg):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def err(msg, exc=None):
    log(f"[ERR] {msg}")
    if exc is not None:
        print(traceback.format_exc(), flush=True)


# ---------- telegram ----------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": TELEGRAM_CHAT_ID, "text": message[:4000],
         "parse_mode": "Markdown"}).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True
        except Exception as e:
            err(f"Telegram error ({attempt + 1}/3): {e}")
            time.sleep(2)
    return False


def dump_once(tag, obj):
    if tag in _dumps_done:
        return
    _dumps_done.add(tag)
    try:
        body = json.dumps(obj, indent=1)[:1500]
    except Exception:
        body = str(obj)[:1500]
    log(f"--- FIRST {tag} PAYLOAD ---")
    print(body, flush=True)
    log(f"--- END {tag} ---")
    send_telegram(f"📋 First `{tag}` payload:\n```\n{body[:900]}\n```")


# ---------- api ----------
def api_get(path, label):
    global _rate_limited_until
    url = f"{SPORTBEX_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "sportbex-api-key": SPORTBEX_API_KEY,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    API_STATS["calls"] += 1
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        API_STATS["errors"] += 1
        if e.code == 429:
            _rate_limited_until = time.time() + 30
            err(f"RATE LIMITED on {label} — backing off 30s")
        elif e.code in (401, 403):
            err(f"AUTH FAILED ({e.code}) on {label} — check key / base URL")
        elif e.code == 404:
            err(f"404 on {label} — is SPORTBEX_BASE right? {SPORTBEX_BASE}")
        else:
            err(f"HTTP {e.code} on {label}")
        raise
    except Exception as e:
        API_STATS["errors"] += 1
        err(f"{label} failed: {type(e).__name__}: {e}")
        raise


def fetch_many(tasks):
    """tasks: list of (key, path, label). Returns {key: json|None}, parallel."""
    out = {}
    if not tasks:
        return out
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(tasks))) as ex:
        futures = {ex.submit(api_get, p, l): k for k, p, l in tasks}
        for fut, key in futures.items():
            try:
                out[key] = fut.result()
            except Exception:
                out[key] = None
    return out


# ---------- helpers ----------
def g(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def as_list(obj, *container_keys):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in container_keys:
            v = obj.get(k)
            if isinstance(v, list):
                return v
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def unwrap(payload):
    """market-odds comes back as {"status": true, "data": {...}}."""
    if isinstance(payload, dict):
        d = payload.get("data")
        if isinstance(d, dict):
            return d
        if isinstance(d, list) and d and isinstance(d[0], dict):
            return d[0]
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt(v):
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def first_price(runner, side):
    """FIRST entry of the runner's back/lay array.

      "back": [{"price": 3.35, "size": 218}, {"price": 3.3, ...}, ...]
                 ^^^^^^^^^^^^ best available (array is descending)
    """
    arr = g(runner, side, default=None)
    if not isinstance(arr, list) or not arr:
        return None
    entry = arr[0]
    if isinstance(entry, dict):
        try:
            return float(g(entry, "price", "odds"))
        except (TypeError, ValueError):
            return None
    try:
        return float(entry)
    except (TypeError, ValueError):
        return None


# ---------- persistence ----------
def load_state():
    if not os.path.exists(STATE_FILE):
        log("No state file — cold start.")
        return
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        now = time.time()
        for k, v in (d.get("alerted") or {}).items():
            try:
                if now - float(v) < SEEN_TTL_SECONDS:
                    alerted[str(k)] = float(v)
            except Exception:
                continue
        log(f"Loaded {len(alerted)} previously alerted.")
    except Exception as e:
        err(f"State load failed: {e}", e)


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"alerted": alerted}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        err(f"State save failed: {e}", e)


# ---------- discovery ----------
def pick_race_markets(markets, fallback_dt):
    """market-all-list returns markets for a WHOLE MEETING.
    One race per distinct marketStartTime; drop place/forecast markets;
    at each start time keep the market with the most runners."""
    by_time = {}
    skipped = set()
    for m in markets:
        mid = g(m, "marketId", "id", "market_id")
        if not mid:
            continue
        mname = str(g(m, "marketName", "name", default="") or "")
        if any(k in mname.lower() for k in NON_WIN_KEYWORDS):
            skipped.add(mname)
            continue

        mstart = parse_iso(g(m, "marketStartTime", "startTime")) or fallback_dt
        runners = {}
        for r in as_list(g(m, "runners", default=[]) or []):
            sid = g(r, "selectionId", "selection_id", "id")
            if sid is None:
                continue
            rname = str(g(r, "runnerName", "runner_name", "name",
                          default=f"Selection {sid}"))
            meta = g(r, "metadata", default={}) or {}
            cloth = str(g(meta, "CLOTH_NUMBER", default="") or "").strip()
            runners[str(sid)] = f"{cloth} {rname}".strip() if cloth else rname

        key = mstart.isoformat() if mstart else str(mid)
        cand = {"market_id": str(mid), "market_name": mname,
                "runners": runners, "start": mstart}
        if key not in by_time or len(runners) > len(by_time[key]["runners"]):
            by_time[key] = cand
    return list(by_time.values()), skipped


def register(market, event, competition):
    global _reg_counter
    mid = market["market_id"]
    if mid in registry:
        return False

    venue = str(g(event, "venue", default="") or
                g(competition, "name", default="?"))
    country = str(g(event, "countryCode", "country_code", default="") or "")
    tzname = str(g(event, "timezone", default="") or "Europe/London")
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = UK_TZ

    start = market["start"]
    local = start.astimezone(tz) if start else None
    race_label = (f"{local.strftime('%H:%M')} {venue}" if local
                  else f"{venue} {market['market_name']}")

    _reg_counter += 1
    now_e = time.time()
    registry[mid] = {
        "event_id": str(g(event, "id", "eventId", default="")),
        "race_label": race_label,
        "market_name": market["market_name"],
        "venue": venue,
        "country": country,
        "race_epoch": start.timestamp() if start else now_e + 86400,
        "race_time": (local.strftime("%H:%M %d-%b") + f" ({tzname})"
                      if local else "unknown"),
        "runners": market["runners"],
        # stagger first polls across the poll window
        "last_poll": now_e - POLL_SECONDS + (_reg_counter % POLL_SECONDS),
    }
    return True


def discover():
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # STEP 1 — every horse racing competition
    try:
        comps_raw = api_get(f"/betfair/competition-list/{SPORT_ID}",
                            "competition-list")
    except Exception:
        return
    dump_once("competition-list", comps_raw)

    comps = as_list(comps_raw, "competitions", "result", "data")
    comp_tasks, comp_meta = [], {}
    regions = set()
    for c in comps:
        comp = g(c, "competition", default=c) or {}
        cid = g(comp, "id", "competitionId")
        if not cid:
            continue
        cid = str(cid)
        comp_meta[cid] = comp
        regions.add(str(g(c, "competitionRegion", "region", default="?")))
        comp_tasks.append(
            (cid, f"/betfair/racing-event-list/{SPORT_ID}/{cid}",
             f"events {g(comp, 'name', default=cid)}"))

    # STEP 2 — events per competition (parallel)
    events_by_comp = fetch_many(comp_tasks)
    for v in events_by_comp.values():
        if v:
            dump_once("racing-event-list", v)
            break

    market_tasks, event_meta = [], {}
    now_e = time.time()
    total_events = skipped_events = 0
    for cid, raw in events_by_comp.items():
        if not raw:
            continue
        for e in as_list(raw, "events", "result", "data"):
            ev = g(e, "event", default=e) or {}
            eid = g(ev, "id", "eventId")
            if not eid:
                continue
            eid = str(eid)
            total_events += 1

            ename = str(g(ev, "name", default="") or "").lower()
            if any(bad in ename for bad in BAD_EVENT_MARKERS):
                skipped_events += 1
                continue

            open_dt = parse_iso(g(ev, "openDate", "open_date", "startTime"))
            if open_dt:
                if MAX_DAYS_AHEAD > 0:
                    days = (open_dt - now_utc).total_seconds() / 86400.0
                    if days > MAX_DAYS_AHEAD:
                        continue
                # meeting finished long ago
                if (now_utc - open_dt).total_seconds() > 8 * 3600:
                    continue
            if now_e - event_fetched.get(eid, 0) < EVENT_REFETCH_SECONDS:
                continue

            event_meta[eid] = (ev, comp_meta.get(cid, {}), open_dt)
            market_tasks.append(
                (eid, f"/betfair/market-all-list/{eid}",
                 f"markets {g(ev, 'name', default=eid)}"))

    # STEP 3 — markets per event (parallel)
    markets_by_event = fetch_many(market_tasks)
    added = 0
    for eid, raw in markets_by_event.items():
        event_fetched[eid] = now_e
        if not raw:
            continue
        dump_once("market-all-list", raw)
        ev, comp, open_dt = event_meta[eid]
        races, skipped = pick_race_markets(
            as_list(raw, "markets", "result", "data"), open_dt)
        _skipped_names.update(skipped)
        for race in races:
            if register(race, ev, comp):
                added += 1

    # purge finished races
    for mid in [m for m, v in registry.items()
                if now_e > v["race_epoch"] + PURGE_AFTER_OFF]:
        registry.pop(mid, None)
        for k in [k for k in list(runner_state) if k.startswith(f"{mid}:")]:
            runner_state.pop(k, None)

    log(f"DISCOVER: competitions={len(comps)} regions={sorted(regions)} "
        f"events={total_events} skipped_events={skipped_events} "
        f"markets_fetched={len(market_tasks)} new_races={added} "
        f"registry={len(registry)}")
    if _skipped_names and "skipped" not in _dumps_done:
        _dumps_done.add("skipped")
        log(f"NON-WIN MARKET NAMES EXCLUDED: {sorted(_skipped_names)[:30]}")


# ---------- process one market's odds ----------
def process_market(mid, info, odds_raw):
    if odds_raw is None:
        return 0
    dump_once("market-odds", odds_raw)

    book = unwrap(odds_raw)
    mkt_status = str(g(book, "status", default="") or "").upper()
    if mkt_status:
        _mkt_statuses_seen[mkt_status] = _mkt_statuses_seen.get(
            mkt_status, 0) + 1
    if mkt_status in DEAD_MARKET_STATUSES:
        registry.pop(mid, None)          # race settled — stop watching
        return 0

    runners = as_list(g(book, "runners", default=None), "runners")
    if not runners:
        return 0

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_e = time.time()
    alerts = 0

    for r in runners:
        sid = g(r, "selectionId", "selection_id", "id")
        if sid is None:
            continue
        sid = str(sid)
        key = f"{mid}:{sid}"
        name = info["runners"].get(sid) or str(
            g(r, "runnerName", "name", default=f"Selection {sid}"))

        status = str(g(r, "status", default="") or "").upper().strip()
        if status:
            _statuses_seen[status] = _statuses_seen.get(status, 0) + 1

        back = first_price(r, "back")
        lay = first_price(r, "lay")
        prev = runner_state.get(key)

        # ---- ACTIVE: refresh the stored snapshot ----
        if status == "ACTIVE":
            hist = (prev or {}).get("hist", [])
            if back:
                hist = hist + [{"t": now_utc.strftime("%H:%M"),
                                "p": round(back, 2)}]
            runner_state[key] = {
                "status": status,
                "back": back if back else (prev or {}).get("back"),
                "lay": lay if lay else (prev or {}).get("lay"),
                "ts": now_utc.strftime("%d-%b %H:%M:%S"),
                "epoch": now_e,
                "hist": hist[-5:],
            }
            continue

        # ---- NOT ACTIVE ----
        if prev is None:
            # already non-active at first sighting = pre-existing
            runner_state[key] = {"status": status, "back": back, "lay": lay,
                                 "ts": now_utc.strftime("%d-%b %H:%M:%S"),
                                 "epoch": now_e, "hist": []}
            continue

        was = prev.get("status")
        runner_state[key]["status"] = status
        if was != "ACTIVE" or key in alerted:
            continue

        # ---- ACTIVE -> non-ACTIVE : trigger ----
        price = back or prev.get("back")   # removed runners return no prices

        if not price:
            alerted[key] = now_e
            log(f"FILTERED: {name} @ {info['race_label']} "
                f"[ACTIVE -> {status}] — no back price ever stored")
            continue
        if float(price) >= MAX_ODDS:
            alerted[key] = now_e
            log(f"FILTERED: {name} @ {info['race_label']} "
                f"[ACTIVE -> {status}] — back {float(price):.2f} "
                f"not < {MAX_ODDS}")
            continue

        rf = f"~{(1 / float(price)) * 100:.1f}%"
        hist = prev.get("hist", [])
        trend = ""
        if len(hist) >= 2:
            trend = "📈 *Move:* `" + " → ".join(
                str(h["p"]) for h in hist) + "`\n"
        age = int((now_e - prev.get("epoch", now_e)) / 60)
        lay_line = (f"📘 *Lay:* `{fmt(prev.get('lay'))}`\n"
                    if prev.get("lay") else "")
        ctry = f" [{info['country']}]" if info.get("country") else ""

        msg = (
            f"⚡ *NON-RUNNER DETECTED*\n\n"
            f"🏇 *Horse:* {name}\n"
            f"📍 *Race:* {info['race_label']}{ctry}\n"
            f"🏁 *Market:* {info['market_name']}\n"
            f"🔄 *Status:* `ACTIVE ➜ {status}`\n"
            f"📊 *Back Price:* `{fmt(price)}`\n"
            f"{lay_line}"
            f"{trend}"
            f"📉 *Reduction Factor:* `{rf}`\n"
            f"🕐 *Price from:* `{prev.get('ts')} UTC` ({age}m before)\n"
            f"⏰ *Race Time:* {info['race_time']}"
        )
        log(f"ALERT: {name} @ {info['race_label']} "
            f"[ACTIVE -> {status}] back={fmt(price)}")
        if send_telegram(msg):
            alerted[key] = now_e
            alerts += 1
            save_state()

    return alerts


def poll_cycle():
    now_e = time.time()
    if now_e < _rate_limited_until:
        return

    due = []
    for mid, info in registry.items():
        if info["race_epoch"] - now_e < -STOP_POLL_AFTER_OFF:
            continue
        if now_e - info["last_poll"] < POLL_SECONDS:
            continue
        due.append(mid)
    if not due:
        return

    for mid in due:
        registry[mid]["last_poll"] = now_e

    results = fetch_many([(mid, f"/racing/market-odds/{mid}", f"odds {mid}")
                          for mid in due])

    alerts = 0
    for mid, odds_raw in results.items():
        info = registry.get(mid)
        if info:
            alerts += process_market(mid, info, odds_raw)

    log(f"CYCLE: polled={len(due)} registry={len(registry)} "
        f"tracked={len(runner_state)} alerts={alerts} "
        f"runner_statuses={dict(sorted(_statuses_seen.items()))} "
        f"market_statuses={dict(sorted(_mkt_statuses_seen.items()))} "
        f"api_calls={API_STATS['calls']} api_errors={API_STATS['errors']}")


if __name__ == "__main__":
    log("=== RUNNER 2 — SPORTBEX / BETFAIR NON-RUNNER MONITOR ===")
    log(f"Base: {SPORTBEX_BASE}")
    log("Scope: ALL horse racing, ALL regions, ALL dates"
        + (f" (capped {MAX_DAYS_AHEAD}d)" if MAX_DAYS_AHEAD > 0 else ""))
    log("Trigger: runner ACTIVE -> ANY other status")
    log(f"Condition: last fetched FIRST back price < {MAX_ODDS}")
    log(f"Polling: {POLL_SECONDS}s per market | {WORKERS} workers | "
        f"tick {TICK}s | skip markets with status in "
        f"{sorted(DEAD_MARKET_STATUSES)}")
    load_state()

    log("Initial discovery...")
    try:
        discover()
    except Exception as e:
        err(f"Initial discovery failed: {e}", e)

    log("Baseline sweep (recording current statuses, no alerts)...")
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            poll_cycle()
        except Exception as e:
            err(f"Baseline error: {e}", e)
        if registry and all(time.time() - v["last_poll"] < POLL_SECONDS
                            for v in registry.values()):
            break
        time.sleep(TICK)
    save_state()

    send_telegram(f"⚡ SportBex non-runner monitor LIVE\n"
                  f"{len(registry)} races registered, "
                  f"{len(runner_state)} runners baselined.\n"
                  f"Trigger: ACTIVE ➜ non-ACTIVE, back price < {MAX_ODDS}\n"
                  f"Polling every {POLL_SECONDS}s")
    log("Alerting live.")

    last_discover = time.time()
    while True:
        try:
            poll_cycle()
            if time.time() - last_discover >= DISCOVER_SECONDS:
                last_discover = time.time()
                discover()
                save_state()
        except KeyboardInterrupt:
            save_state()
            log("Stopped.")
            break
        except Exception as e:
            err(f"Loop error: {e}", e)
        time.sleep(TICK)
