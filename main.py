import datetime
import json
import os
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

# ---------------- CONFIG ----------------
API_BASE = os.environ.get("API_BASE", "http://157.245.44.178/api")
API_BASE_ALT = os.environ.get("API_BASE_ALT", "http://167.99.82.136/api")
API_KEY = os.environ.get("API_KEY", "").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8435489741")

SPORT_ID = 7
REGIONS = os.environ.get("REGIONS", "ALL")
ALLOWED_REGIONS = (set() if REGIONS.strip().upper() == "ALL"
                   else {r.strip().upper() for r in REGIONS.split(",")
                         if r.strip()})

MAX_ODDS = float(os.environ.get("MAX_ODDS", "6.0"))
LAY_FALLBACK = os.environ.get("LAY_FALLBACK", "0") == "1"
OFFICIAL_RF = os.environ.get("OFFICIAL_RF", "1") == "1"

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))
WORKERS = int(os.environ.get("WORKERS", "16"))
TICK = 5
DISCOVER_SECONDS = int(os.environ.get("DISCOVER_SECONDS", "600"))
EVENT_REFETCH_SECONDS = int(os.environ.get("EVENT_REFETCH_SECONDS", "1800"))

HARD_STOP_AFTER_OFF = 6 * 3600
EVENT_PAST_CUTOFF = 24 * 3600
MARKET_PAST_GRACE = 900
MAX_DAYS_AHEAD = float(os.environ.get("MAX_DAYS_AHEAD", "0"))

STATE_FILE = os.environ.get("STATE_FILE", "/opt/nrbot/nr_state.json")
STATE_SAVE_EVERY = 20
RUNNER_STATE_TTL = 4 * 86400
SEEN_TTL_SECONDS = 4 * 86400
FAIL_ALERT_AFTER = int(os.environ.get("FAIL_ALERT_AFTER", "6"))
PROBE_NEW_ENDPOINTS = os.environ.get("PROBE_NEW_ENDPOINTS", "0") == "1"

UK_TZ = ZoneInfo("Europe/London")

BAD_EVENT_MARKERS = ("(rfc)", "(f/c)", "(fc)", "(tri)", "(tricast)",
                     "antepost", "ante-post", "ante post")

NON_WIN_KEYWORDS = (
    "to be placed", "place", "forecast", "tricast", "match bet",
    "without", "winning distance", "number of", "insurance",
    "each way", "reverse", "double", "treble", "jockey", "trainer",
    "winner of", "favourite", "unnamed", "starting price", "outsider",
    "margin", "distance betting", "hi/lo", "under/over",
)

DEAD_MARKET_STATUSES = {"CLOSED", "SETTLED", "VOIDED", "CANCELLED"}

registry = {}
runner_state = {}
alerted = {}
event_fetched = {}
market_fail = {}
fail_warned = set()
recon_warned = set()
_dumps_done = set()
_skipped_names = set()
_statuses_seen = {}
_mkt_statuses_seen = {}
_rate_limited_until = 0.0
_reg_counter = 0
_cycle_count = 0
_active_base = API_BASE
STATS = {"calls": 0, "errors": 0, "recon_mismatch": 0, "vanished": 0,
         "filtered_no_price": 0, "filtered_odds": 0, "place_dropped": 0,
         "official_rf": 0, "derived_rf": 0}


def log(msg):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print("[" + stamp + "] " + msg, flush=True)


def err(msg, exc=None):
    log("[ERR] " + msg)
    if exc is not None:
        print(traceback.format_exc(), flush=True)


# ---------------- telegram ----------------
def send_telegram(message):
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
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
            err("Telegram error (" + str(attempt + 1) + "/3): " + str(e))
            time.sleep(2)
    return False


def dump_once(tag, obj, limit=1800):
    if tag in _dumps_done:
        return
    _dumps_done.add(tag)
    try:
        body = json.dumps(obj, indent=1)[:limit]
    except Exception:
        body = str(obj)[:limit]
    log("--- FIRST " + tag + " PAYLOAD ---")
    print(body, flush=True)
    log("--- END " + tag + " ---")
    send_telegram("📋 `" + tag + "`:\n```\n" + body[:900] + "\n```")


# ---------------- api ----------------
def _headers():
    h = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    if API_KEY:
        h["sportbex-api-key"] = API_KEY
    return h


def api_get(path, label):
    global _rate_limited_until
    req = urllib.request.Request(_active_base + path, headers=_headers())
    STATS["calls"] += 1
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        STATS["errors"] += 1
        if e.code == 429:
            _rate_limited_until = time.time() + 30
            err("RATE LIMITED on " + label + " — backing off 30s")
        elif e.code in (401, 403):
            err("AUTH FAILED (" + str(e.code) + ") on " + label)
        elif e.code == 404:
            err("404 on " + label + " — check API_BASE (" + _active_base + ")")
        else:
            err("HTTP " + str(e.code) + " on " + label)
        raise
    except Exception as e:
        STATS["errors"] += 1
        err(label + " failed: " + type(e).__name__ + ": " + str(e))
        raise


def api_post(path, body, label):
    data = json.dumps(body).encode("utf-8")
    h = _headers()
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(_active_base + path, data=data,
                                 headers=h, method="POST")
    STATS["calls"] += 1
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        STATS["errors"] += 1
        err(label + " failed: " + type(e).__name__ + ": " + str(e))
        raise


def fetch_many(tasks):
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


def choose_base():
    global _active_base
    for base in (API_BASE, API_BASE_ALT):
        _active_base = base
        try:
            api_get("/betfair/competition-list/" + str(SPORT_ID), "base probe")
            log("API base OK: " + base)
            return True
        except Exception:
            log("API base unreachable: " + base)
    _active_base = API_BASE
    err("NEITHER API base responded")
    return False


# ---------------- helpers ----------------
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
    """market-odds arrives as {"status": true, "data": {...}}."""
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


def safe_tz(name):
    try:
        return ZoneInfo(name)
    except Exception:
        return UK_TZ


def fmt(v):
    try:
        return "{:.2f}".format(float(v))
    except (TypeError, ValueError):
        return "N/A"


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def first_price(runner, side):
    """FIRST entry of the runner's back/lay array — the best available."""
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


# -------- official Betfair reduction factor, fetched at alert time --------
def fetch_official_rf(mid, sid):
    """POST the single marketId to market-listMarketBook and read the
    runner's adjustmentFactor. Raw Betfair format: no runnerName, prices
    under ex.availableToBack, and adjustmentFactor per runner."""
    if not OFFICIAL_RF:
        return None
    try:
        raw = api_post("/racing/market-listMarketBook", {"marketIds": [mid]},
                       "listMarketBook " + mid)
    except Exception:
        return None

    books = raw.get("data") if isinstance(raw, dict) else raw
    if isinstance(books, dict):
        books = [books]
    if not isinstance(books, list):
        return None

    for b in books:
        if not isinstance(b, dict):
            continue
        if str(g(b, "marketId", default="")) not in ("", str(mid)):
            continue
        for r in as_list(g(b, "runners", default=None), "runners"):
            rid = g(r, "selectionId", "selection_id", "id")
            if rid is None or str(rid) != str(sid):
                continue
            try:
                af = float(g(r, "adjustmentFactor", "adjustment_factor"))
            except (TypeError, ValueError):
                return None
            return af if af > 0 else None
    return None


# ---------------- state persistence ----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        log("No state file — cold start (first run will baseline).")
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
        kept = 0
        for k, v in (d.get("runner_state") or {}).items():
            if not isinstance(v, dict):
                continue
            try:
                if now - float(v.get("epoch", 0)) < RUNNER_STATE_TTL:
                    runner_state[str(k)] = v
                    kept += 1
            except Exception:
                continue
        log("State restored: " + str(len(alerted)) + " alerted, " + str(kept)
            + " runner statuses — transitions during downtime will alert.")
    except Exception as e:
        err("State load failed: " + str(e), e)


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"alerted": alerted, "runner_state": runner_state}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        err("State save failed: " + str(e), e)


def prune_state():
    now = time.time()
    for k in [k for k, v in runner_state.items()
              if now - float(v.get("epoch", now)) > RUNNER_STATE_TTL]:
        runner_state.pop(k, None)
    for k in [k for k, t in alerted.items()
              if now - float(t) > SEEN_TTL_SECONDS]:
        alerted.pop(k, None)


# ---------------- discovery ----------------
def pick_race_markets(markets, fallback_dt):
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
                          default="Selection " + str(sid)))
            meta = g(r, "metadata", default={}) or {}
            cloth = str(g(meta, "CLOTH_NUMBER", default="") or "").strip()
            runners[str(sid)] = ((cloth + " " + rname).strip()
                                 if cloth else rname)
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

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    start = market["start"]
    if start:
        if (now_utc - start).total_seconds() > MARKET_PAST_GRACE:
            return False
        if MAX_DAYS_AHEAD > 0 and \
                (start - now_utc).total_seconds() / 86400.0 > MAX_DAYS_AHEAD:
            return False

    venue = str(g(event, "venue", default="")
                or g(competition, "name", default="?"))
    country = str(g(event, "countryCode", "country_code", default="") or "")
    tzname = str(g(event, "timezone", default="") or "Europe/London")
    tz = safe_tz(tzname)

    local = start.astimezone(tz) if start else None
    race_label = (local.strftime("%H:%M") + " " + venue if local
                  else venue + " " + market["market_name"])

    _reg_counter += 1
    now_e = time.time()
    registry[mid] = {
        "event_id": str(g(event, "id", "eventId", default="")),
        "race_label": race_label,
        "market_name": market["market_name"],
        "venue": venue,
        "country": country,
        "race_epoch": start.timestamp() if start else now_e + 86400,
        "race_time": (local.strftime("%H:%M %d-%b") + " (" + tzname + ")"
                      if local else "unknown"),
        "runners": market["runners"],
        "last_poll": now_e - POLL_SECONDS + (_reg_counter % POLL_SECONDS),
    }
    return True


def discover():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    try:
        comps_raw = api_get("/betfair/competition-list/" + str(SPORT_ID),
                            "competition-list")
    except Exception:
        err("DISCOVERY ABORTED — competition-list unavailable this round")
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
        region = str(g(c, "competitionRegion", "region", default="") or "")
        regions.add(region or "?")
        if ALLOWED_REGIONS and region.upper() not in ALLOWED_REGIONS:
            continue
        comp_meta[cid] = comp
        comp_tasks.append(
            (cid, "/betfair/racing-event-list/" + str(SPORT_ID) + "/" + cid,
             "events " + str(g(comp, "name", default=cid))))

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
            if open_dt and (now_utc - open_dt).total_seconds() > EVENT_PAST_CUTOFF:
                continue
            if now_e - event_fetched.get(eid, 0) < EVENT_REFETCH_SECONDS:
                continue
            event_meta[eid] = (ev, comp_meta.get(cid, {}), open_dt)
            market_tasks.append(
                (eid, "/betfair/market-all-list/" + eid,
                 "markets " + str(g(ev, "name", default=eid))))

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

    prune_state()
    log("DISCOVER: competitions=" + str(len(comps))
        + " regions=" + str(sorted(regions))
        + " events=" + str(total_events)
        + " skipped_events=" + str(skipped_events)
        + " markets_fetched=" + str(len(market_tasks))
        + " new_races=" + str(added)
        + " registry=" + str(len(registry)))
    if _skipped_names and "skipped" not in _dumps_done:
        _dumps_done.add("skipped")
        log("NON-WIN MARKET NAMES EXCLUDED: "
            + str(sorted(_skipped_names)[:30]))


def probe_new_endpoints():
    if not PROBE_NEW_ENDPOINTS or not registry:
        return
    sample = list(registry.keys())[:2]
    log("Probing POST endpoints with marketIds=" + str(sample))
    for path, tag in (("/racing/market-bulk-odds", "market-bulk-odds"),
                      ("/racing/market-listMarketBook",
                       "market-listMarketBook")):
        try:
            dump_once(tag, api_post(path, {"marketIds": sample}, tag),
                      limit=2500)
        except Exception:
            send_telegram("⚠️ Probe of `" + path + "` failed — see logs.")


# ---------------- alerting ----------------
def fire_alert(info, key, name, status, price, price_side, prev):
    mid, sid = key.split(":", 1)

    official = fetch_official_rf(mid, sid)
    if official is not None:
        STATS["official_rf"] += 1
        rf_line = ("📉 *Reduction Factor:* `{:.2f}%`".format(official)
                   + " _(official Betfair)_\n")
    else:
        STATS["derived_rf"] += 1
        rf_line = ("📉 *Reduction Factor:* `~{:.1f}%`".format(
            (1.0 / float(price)) * 100.0) + " _(derived from price)_\n")

    hist = (prev or {}).get("hist", [])
    trend = ""
    if len(hist) >= 2:
        trend = "📈 *Move:* `" + " → ".join(
            str(h["p"]) for h in hist) + "`\n"
    age = int((time.time() - (prev or {}).get("epoch", time.time())) / 60)
    lay_line = ""
    if (prev or {}).get("lay") and price_side == "back":
        lay_line = "📘 *Lay:* `" + fmt((prev or {}).get("lay")) + "`\n"
    ctry = " [" + info["country"] + "]" if info.get("country") else ""
    side_label = ("Back Price" if price_side == "back"
                  else "Lay Price (no back seen)")

    msg = ("⚡ *NON-RUNNER DETECTED*\n\n"
           + "🏇 *Horse:* " + name + "\n"
           + "📍 *Race:* " + info["race_label"] + ctry + "\n"
           + "🏁 *Market:* " + info["market_name"] + "\n"
           + "🔄 *Status:* `ACTIVE ➜ " + status + "`\n"
           + "📊 *" + side_label + ":* `" + fmt(price) + "`\n"
           + lay_line + trend + rf_line
           + "🕐 *Price from:* `" + str((prev or {}).get("ts")) + " UTC` ("
           + str(age) + "m before)\n"
           + "⏰ *Race Time:* " + info["race_time"])

    log("ALERT: " + name + " @ " + info["race_label"]
        + " [ACTIVE -> " + status + "] " + price_side + "=" + fmt(price)
        + " rf=" + ("official " + "{:.2f}".format(official)
                    if official is not None else "derived"))
    if send_telegram(msg):
        alerted[key] = time.time()
        save_state()
        return 1
    return 0


def evaluate_transition(info, key, name, status, back, prev):
    price = back or (prev or {}).get("back")
    side = "back"
    if not price and LAY_FALLBACK:
        price = (prev or {}).get("lay")
        side = "lay"

    if not price:
        alerted[key] = time.time()
        STATS["filtered_no_price"] += 1
        log("FILTERED: " + name + " @ " + info["race_label"]
            + " [ACTIVE -> " + status + "] — no back price ever stored")
        return 0
    if float(price) >= MAX_ODDS:
        alerted[key] = time.time()
        STATS["filtered_odds"] += 1
        log("FILTERED: " + name + " @ " + info["race_label"]
            + " [ACTIVE -> " + status + "] — " + side + " "
            + fmt(price) + " not < " + str(MAX_ODDS))
        return 0
    return fire_alert(info, key, name, status, price, side, prev)


# ---------------- process one market ----------------
def process_market(mid, info, odds_raw):
    if odds_raw is None:
        market_fail[mid] = market_fail.get(mid, 0) + 1
        n = market_fail[mid]
        if n == FAIL_ALERT_AFTER and mid not in fail_warned:
            fail_warned.add(mid)
            log("MARKET FAILING: " + info["race_label"] + " (" + mid + ") — "
                + str(n) + " consecutive fetch failures")
            send_telegram("⚠️ *Market not responding*\n" + info["race_label"]
                          + "\n`" + mid + "` — " + str(n)
                          + " consecutive failures, currently unwatched.")
        return 0
    market_fail[mid] = 0

    dump_once("market-odds", odds_raw)
    book = unwrap(odds_raw)

    mkt_status = str(g(book, "status", default="") or "").upper()
    if mkt_status:
        _mkt_statuses_seen[mkt_status] = _mkt_statuses_seen.get(
            mkt_status, 0) + 1
    if mkt_status in DEAD_MARKET_STATUSES:
        registry.pop(mid, None)
        return 0

    n_winners = as_int(g(book, "numberOfWinners"))
    if n_winners is not None and n_winners > 1:
        STATS["place_dropped"] += 1
        log("PLACE MARKET DROPPED: " + info["race_label"] + " ("
            + info["market_name"] + ") numberOfWinners=" + str(n_winners))
        registry.pop(mid, None)
        return 0

    runners = as_list(g(book, "runners", default=None), "runners")
    if not runners:
        return 0

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_e = time.time()
    alerts = 0
    listed_ids = set()
    listed_active = 0

    for r in runners:
        sid = g(r, "selectionId", "selection_id", "id")
        if sid is None:
            continue
        sid = str(sid)
        listed_ids.add(sid)
        key = mid + ":" + sid
        name = info["runners"].get(sid) or str(
            g(r, "runnerName", "name", default="Selection " + sid))

        status = str(g(r, "status", default="") or "").upper().strip()
        if status:
            _statuses_seen[status] = _statuses_seen.get(status, 0) + 1
        if status == "ACTIVE":
            listed_active += 1

        back = first_price(r, "back")
        lay = first_price(r, "lay")
        prev = runner_state.get(key)

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

        if prev is None:
            runner_state[key] = {"status": status, "back": back, "lay": lay,
                                 "ts": now_utc.strftime("%d-%b %H:%M:%S"),
                                 "epoch": now_e, "hist": []}
            continue

        was = prev.get("status")
        runner_state[key]["status"] = status
        runner_state[key]["epoch"] = now_e
        if was != "ACTIVE" or key in alerted:
            continue
        alerts += evaluate_transition(info, key, name, status, back, prev)

    n_total = as_int(g(book, "numberOfRunners"))
    n_active = as_int(g(book, "numberOfActiveRunners"))
    listed = len(listed_ids)

    if n_total is not None and listed != n_total and mid not in recon_warned:
        recon_warned.add(mid)
        STATS["recon_mismatch"] += 1
        log("RECON MISMATCH: " + info["race_label"] + " (" + mid
            + ") — payload lists " + str(listed)
            + " runners but numberOfRunners=" + str(n_total))
    if n_active is not None and listed_active != n_active \
            and mid not in recon_warned:
        recon_warned.add(mid)
        STATS["recon_mismatch"] += 1
        log("RECON MISMATCH: " + info["race_label"] + " (" + mid + ") — "
            + str(listed_active) + " ACTIVE listed but "
            + "numberOfActiveRunners=" + str(n_active))

    if n_total is not None and listed == n_total:
        for sid, name in info["runners"].items():
            if sid in listed_ids:
                continue
            key = mid + ":" + sid
            prev = runner_state.get(key)
            if not prev or prev.get("status") != "ACTIVE" or key in alerted:
                continue
            STATS["vanished"] += 1
            log("VANISHED: " + name + " @ " + info["race_label"]
                + " — was ACTIVE, no longer listed")
            runner_state[key]["status"] = "VANISHED"
            runner_state[key]["epoch"] = now_e
            alerts += evaluate_transition(info, key, name, "VANISHED",
                                          None, prev)
    return alerts


def poll_cycle():
    global _cycle_count
    now_e = time.time()
    if now_e < _rate_limited_until:
        return

    due = []
    for mid, info in registry.items():
        if info["race_epoch"] - now_e < -HARD_STOP_AFTER_OFF:
            continue
        if now_e - info["last_poll"] < POLL_SECONDS:
            continue
        due.append(mid)
    if not due:
        return

    for mid in due:
        registry[mid]["last_poll"] = now_e

    results = fetch_many([(mid, "/racing/market-odds/" + mid, "odds " + mid)
                          for mid in due])

    alerts = 0
    for mid, odds_raw in results.items():
        info = registry.get(mid)
        if info:
            alerts += process_market(mid, info, odds_raw)

    for mid in [m for m, v in registry.items()
                if now_e > v["race_epoch"] + HARD_STOP_AFTER_OFF]:
        registry.pop(mid, None)

    _cycle_count += 1
    if _cycle_count % STATE_SAVE_EVERY == 0:
        save_state()

    failing = sum(1 for n in market_fail.values() if n >= FAIL_ALERT_AFTER)
    log("CYCLE: polled=" + str(len(due))
        + " registry=" + str(len(registry))
        + " tracked=" + str(len(runner_state))
        + " alerts=" + str(alerts)
        + " failing=" + str(failing)
        + " no_price=" + str(STATS["filtered_no_price"])
        + " odds_filtered=" + str(STATS["filtered_odds"])
        + " place_dropped=" + str(STATS["place_dropped"])
        + " recon=" + str(STATS["recon_mismatch"])
        + " vanished=" + str(STATS["vanished"])
        + " rf_official=" + str(STATS["official_rf"])
        + " rf_derived=" + str(STATS["derived_rf"])
        + " runner_statuses=" + str(dict(sorted(_statuses_seen.items())))
        + " market_statuses=" + str(dict(sorted(_mkt_statuses_seen.items())))
        + " api_calls=" + str(STATS["calls"])
        + " api_errors=" + str(STATS["errors"]))


if __name__ == "__main__":
    log("=== NON-RUNNER MONITOR — PRODUCTION (DigitalOcean) ===")
    log("Base: " + API_BASE + "  (alt " + API_BASE_ALT + ")")
    log("Auth header: " + ("sent" if API_KEY else "NOT sent (no key)"))
    log("Regions: " + ("ALL" if not ALLOWED_REGIONS
                       else str(sorted(ALLOWED_REGIONS)))
        + " | days ahead: " + ("unlimited" if MAX_DAYS_AHEAD == 0
                               else str(MAX_DAYS_AHEAD)))
    log("Trigger: runner ACTIVE -> ANY other status, or vanished")
    log("Condition: last fetched FIRST back price < " + str(MAX_ODDS)
        + (" (lay fallback ON)" if LAY_FALLBACK else ""))
    log("Reduction factor: " + ("official Betfair adjustmentFactor "
                                "(fallback to derived)" if OFFICIAL_RF
                                else "derived from price only"))
    log("Polling: " + str(POLL_SECONDS) + "s per market | "
        + str(WORKERS) + " workers | tick " + str(TICK) + "s")
    log("State file: " + STATE_FILE)
    load_state()

    choose_base()

    log("Initial discovery...")
    try:
        discover()
    except Exception as e:
        err("Initial discovery failed: " + str(e), e)

    try:
        probe_new_endpoints()
    except Exception as e:
        err("Probe failed: " + str(e), e)

    had_state = len(runner_state) > 0
    log("Baseline sweep..." if not had_state
        else "Reconciling against restored state...")
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            poll_cycle()
        except Exception as e:
            err("Baseline error: " + str(e), e)
        if registry and all(time.time() - v["last_poll"] < POLL_SECONDS
                            for v in registry.values()):
            break
        time.sleep(TICK)
    save_state()

    send_telegram("⚡ Non-runner monitor LIVE "
                  + ("(state restored)" if had_state else "(cold start)")
                  + "\nHost: DigitalOcean 139.59.20.81"
                  + "\n" + str(len(registry)) + " races registered, "
                  + str(len(runner_state)) + " runners tracked."
                  + "\nTrigger: ACTIVE ➜ non-ACTIVE, back price < "
                  + str(MAX_ODDS))
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
            err("Loop error: " + str(e), e)
        time.sleep(TICK)
