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
ALLOWED_REGIONS = set()
if REGIONS.strip().upper() != "ALL":
    for _r in REGIONS.split(","):
        _r = _r.strip().upper()
        if _r:
            ALLOWED_REGIONS.add(_r)

# PRIMARY GATE - official Betfair reduction factor decides on its own.
#   RF >= RF_ALWAYS            -> alert, any field size, any displayed price
#   RF_FLOOR <= RF < RF_ALWAYS -> alert only if remaining <= RF_MID_MAX_RUNNERS
#   RF <  RF_FLOOR             -> never alert
RF_ALWAYS = float(os.environ.get("RF_ALWAYS", "20.0"))
RF_FLOOR = float(os.environ.get("RF_FLOOR", "16.0"))
RF_MID_MAX_RUNNERS = int(os.environ.get("RF_MID_MAX_RUNNERS", "8"))

# FALLBACK ONLY - used when the official RF lookup fails.
MAX_ODDS = float(os.environ.get("MAX_ODDS", "6.0"))
MIN_ODDS = float(os.environ.get("MIN_ODDS", "1.20"))
MIN_ODDS_ALERT_ON_UNKNOWN_RF = os.environ.get("MIN_ODDS_ALERT_ON_UNKNOWN_RF", "1") == "1"

# flag when the displayed price and the official RF disagree badly
PRICE_MISMATCH_RATIO = float(os.environ.get("PRICE_MISMATCH_RATIO", "1.5"))

RF_ATTEMPTS = int(os.environ.get("RF_ATTEMPTS", "2"))
STALE_MINUTES = float(os.environ.get("STALE_MINUTES", "15"))

LEAD_LIMITS_RAW = os.environ.get("LEAD_LIMITS", "US:2")
LEAD_LIMITS = {}
for _part in LEAD_LIMITS_RAW.split(","):
    _part = _part.strip()
    if not _part:
        continue
    if ":" not in _part:
        continue
    _c, _h = _part.split(":", 1)
    try:
        LEAD_LIMITS[_c.strip().upper()] = float(_h.strip()) * 3600.0
    except ValueError:
        pass

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

DEAD_MARKET_STATUSES = ("CLOSED", "SETTLED", "VOIDED", "CANCELLED")

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

STATS = {
    "calls": 0,
    "errors": 0,
    "recon_mismatch": 0,
    "vanished": 0,
    "filtered_no_signal": 0,
    "filtered_odds": 0,
    "filtered_min_odds": 0,
    "filtered_lead_time": 0,
    "filtered_rf_floor": 0,
    "filtered_rf_field": 0,
    "place_dropped": 0,
    "official_rf": 0,
    "derived_rf": 0,
    "price_mismatch": 0,
    "catchup": 0,
}


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
    body = {}
    body["chat_id"] = TELEGRAM_CHAT_ID
    body["text"] = message[:4000]
    body["parse_mode"] = "Markdown"
    payload = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    attempt = 0
    while attempt < 3:
        attempt += 1
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            code = resp.status
            resp.close()
            if code == 200:
                return True
        except Exception as e:
            err("Telegram error " + str(attempt) + "/3: " + str(e))
            time.sleep(2)
    return False


def dump_once(tag, obj, limit=1800):
    if tag in _dumps_done:
        return
    _dumps_done.add(tag)
    try:
        text = json.dumps(obj, indent=1)[:limit]
    except Exception:
        text = str(obj)[:limit]
    log("--- FIRST " + tag + " PAYLOAD ---")
    print(text, flush=True)
    log("--- END " + tag + " ---")
    send_telegram("First " + tag + ":\n```\n" + text[:900] + "\n```")


# ---------------- api ----------------
def build_headers():
    h = {}
    h["Accept"] = "application/json"
    h["User-Agent"] = "Mozilla/5.0"
    if API_KEY:
        h["sportbex-api-key"] = API_KEY
    return h


def api_get(path, label):
    global _rate_limited_until
    url = _active_base + path
    req = urllib.request.Request(url, headers=build_headers())
    STATS["calls"] += 1
    try:
        resp = urllib.request.urlopen(req, timeout=25)
        raw = resp.read().decode("utf-8")
        resp.close()
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        STATS["errors"] += 1
        if e.code == 429:
            _rate_limited_until = time.time() + 30
            err("RATE LIMITED on " + label + " - backing off 30s")
        elif e.code == 401 or e.code == 403:
            err("AUTH FAILED " + str(e.code) + " on " + label)
        elif e.code == 404:
            err("404 on " + label + " - check API_BASE " + _active_base)
        else:
            err("HTTP " + str(e.code) + " on " + label)
        raise
    except Exception as e:
        STATS["errors"] += 1
        err(label + " failed: " + type(e).__name__ + ": " + str(e))
        raise


def api_post(path, body, label):
    url = _active_base + path
    data = json.dumps(body).encode("utf-8")
    h = build_headers()
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    STATS["calls"] += 1
    try:
        resp = urllib.request.urlopen(req, timeout=25)
        raw = resp.read().decode("utf-8")
        resp.close()
        return json.loads(raw)
    except Exception as e:
        STATS["errors"] += 1
        err(label + " failed: " + type(e).__name__ + ": " + str(e))
        raise


def fetch_many(tasks):
    out = {}
    if not tasks:
        return out
    workers = WORKERS
    if len(tasks) < workers:
        workers = len(tasks)
    ex = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    for item in tasks:
        key = item[0]
        path = item[1]
        label = item[2]
        fut = ex.submit(api_get, path, label)
        futures[fut] = key
    for fut in futures:
        key = futures[fut]
        try:
            out[key] = fut.result()
        except Exception:
            out[key] = None
    ex.shutdown(wait=True)
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
def g(d, *keys, **kw):
    default = kw.get("default", None)
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d:
            if d[k] is not None:
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
            if isinstance(v, list):
                if v and isinstance(v[0], dict):
                    return v
    return []


def unwrap(payload):
    if isinstance(payload, dict):
        d = payload.get("data")
        if isinstance(d, dict):
            return d
        if isinstance(d, list):
            if d and isinstance(d[0], dict):
                return d[0]
        return payload
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            return payload[0]
    return {}


def parse_iso(ts):
    if not ts:
        return None
    text = str(ts).replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(text)
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


def pct(v):
    try:
        return "{:.2f}".format(float(v)) + "%"
    except (TypeError, ValueError):
        return "N/A"


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def first_price(runner, side):
    arr = g(runner, side, default=None)
    if not isinstance(arr, list):
        return None
    if not arr:
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


# ---------------- official Betfair reduction factor ----------------
def rf_once(mid, sid):
    body = {"marketIds": [mid]}
    raw = api_post("/racing/market-listMarketBook", body, "listMarketBook")
    books = raw
    if isinstance(raw, dict):
        books = raw.get("data")
    if isinstance(books, dict):
        books = [books]
    if not isinstance(books, list):
        return None
    for b in books:
        if not isinstance(b, dict):
            continue
        bid = str(g(b, "marketId", default=""))
        if bid != "" and bid != str(mid):
            continue
        for r in as_list(g(b, "runners", default=None), "runners"):
            rid = g(r, "selectionId", "selection_id", "id")
            if rid is None:
                continue
            if str(rid) != str(sid):
                continue
            try:
                af = float(g(r, "adjustmentFactor", "adjustment_factor"))
            except (TypeError, ValueError):
                return None
            if af > 0:
                return af
            return None
    return None


def fetch_official_rf(mid, sid):
    if not OFFICIAL_RF:
        return None
    attempts = RF_ATTEMPTS
    if attempts < 1:
        attempts = 1
    attempt = 0
    while attempt < attempts:
        attempt += 1
        af = None
        try:
            af = rf_once(mid, sid)
        except Exception as e:
            if attempt >= attempts:
                err("RF lookup failed after " + str(attempts) + " tries: " + str(e))
        if af is not None:
            return af
        if attempt < attempts:
            time.sleep(1.0)
    return None


# ---------------- state ----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        log("No state file - cold start (first run will baseline).")
        return
    try:
        f = open(STATE_FILE)
        d = json.load(f)
        f.close()
        now = time.time()
        saved = d.get("alerted") or {}
        for k in saved:
            try:
                v = float(saved[k])
            except (TypeError, ValueError):
                continue
            if now - v < SEEN_TTL_SECONDS:
                alerted[str(k)] = v
        kept = 0
        rs = d.get("runner_state") or {}
        for k in rs:
            v = rs[k]
            if not isinstance(v, dict):
                continue
            try:
                epoch = float(v.get("epoch", 0))
            except (TypeError, ValueError):
                continue
            if now - epoch < RUNNER_STATE_TTL:
                runner_state[str(k)] = v
                kept += 1
        log("State restored: " + str(len(alerted)) + " alerted, " + str(kept) + " runner statuses")
    except Exception as e:
        err("State load failed: " + str(e), e)


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        f = open(tmp, "w")
        json.dump({"alerted": alerted, "runner_state": runner_state}, f)
        f.close()
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        err("State save failed: " + str(e), e)


def prune_state():
    now = time.time()
    stale = []
    for k in runner_state:
        try:
            epoch = float(runner_state[k].get("epoch", now))
        except (TypeError, ValueError):
            epoch = now
        if now - epoch > RUNNER_STATE_TTL:
            stale.append(k)
    for k in stale:
        runner_state.pop(k, None)
    old = []
    for k in alerted:
        if now - float(alerted[k]) > SEEN_TTL_SECONDS:
            old.append(k)
    for k in old:
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
        low = mname.lower()
        bad = False
        for kw in NON_WIN_KEYWORDS:
            if kw in low:
                bad = True
                break
        if bad:
            skipped.add(mname)
            continue

        mstart = parse_iso(g(m, "marketStartTime", "startTime"))
        if mstart is None:
            mstart = fallback_dt

        runners = {}
        for r in as_list(g(m, "runners", default=[]) or []):
            sid = g(r, "selectionId", "selection_id", "id")
            if sid is None:
                continue
            rname = str(g(r, "runnerName", "runner_name", "name", default="Selection " + str(sid)))
            meta = g(r, "metadata", default={}) or {}
            cloth = str(g(meta, "CLOTH_NUMBER", default="") or "").strip()
            if cloth:
                if not rname.lstrip().startswith(cloth):
                    rname = cloth + " " + rname
            runners[str(sid)] = rname

        key = str(mid)
        if mstart is not None:
            key = mstart.isoformat()

        cand = {}
        cand["market_id"] = str(mid)
        cand["market_name"] = mname
        cand["runners"] = runners
        cand["start"] = mstart

        if key not in by_time:
            by_time[key] = cand
        elif len(runners) > len(by_time[key]["runners"]):
            by_time[key] = cand

    return list(by_time.values()), skipped


def register(market, event, competition):
    global _reg_counter
    mid = market["market_id"]
    if mid in registry:
        return False

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    start = market["start"]
    if start is not None:
        if (now_utc - start).total_seconds() > MARKET_PAST_GRACE:
            return False
        if MAX_DAYS_AHEAD > 0:
            days = (start - now_utc).total_seconds() / 86400.0
            if days > MAX_DAYS_AHEAD:
                return False

    venue = str(g(event, "venue", default="") or "")
    if not venue:
        venue = str(g(competition, "name", default="?"))
    country = str(g(event, "countryCode", "country_code", default="") or "")
    tzname = str(g(event, "timezone", default="") or "Europe/London")
    tz = safe_tz(tzname)

    local = None
    if start is not None:
        local = start.astimezone(tz)

    if local is not None:
        race_label = local.strftime("%H:%M") + " " + venue
        race_time = local.strftime("%H:%M %d-%b") + " (" + tzname + ")"
        race_epoch = start.timestamp()
    else:
        race_label = venue + " " + market["market_name"]
        race_time = "unknown"
        race_epoch = time.time() + 86400

    _reg_counter += 1
    now_e = time.time()

    entry = {}
    entry["event_id"] = str(g(event, "id", "eventId", default=""))
    entry["race_label"] = race_label
    entry["market_name"] = market["market_name"]
    entry["venue"] = venue
    entry["country"] = country.upper()
    entry["race_epoch"] = race_epoch
    entry["race_time"] = race_time
    entry["runners"] = market["runners"]
    entry["last_poll"] = now_e - POLL_SECONDS + (_reg_counter % POLL_SECONDS)
    registry[mid] = entry
    return True


def discover():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    try:
        comps_raw = api_get("/betfair/competition-list/" + str(SPORT_ID), "competition-list")
    except Exception:
        err("DISCOVERY ABORTED - competition-list unavailable this round")
        return
    dump_once("competition-list", comps_raw)

    comps = as_list(comps_raw, "competitions", "result", "data")
    comp_tasks = []
    comp_meta = {}
    regions = set()

    for c in comps:
        comp = g(c, "competition", default=c) or {}
        cid = g(comp, "id", "competitionId")
        if not cid:
            continue
        cid = str(cid)
        region = str(g(c, "competitionRegion", "region", default="") or "")
        if region:
            regions.add(region)
        else:
            regions.add("?")
        if ALLOWED_REGIONS:
            if region.upper() not in ALLOWED_REGIONS:
                continue
        comp_meta[cid] = comp
        cname = str(g(comp, "name", default=cid))
        path = "/betfair/racing-event-list/" + str(SPORT_ID) + "/" + cid
        comp_tasks.append((cid, path, "events " + cname))

    events_by_comp = fetch_many(comp_tasks)
    for v in events_by_comp.values():
        if v:
            dump_once("racing-event-list", v)
            break

    market_tasks = []
    event_meta = {}
    now_e = time.time()
    total_events = 0
    skipped_events = 0

    for cid in events_by_comp:
        raw = events_by_comp[cid]
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
            bad = False
            for marker in BAD_EVENT_MARKERS:
                if marker in ename:
                    bad = True
                    break
            if bad:
                skipped_events += 1
                continue

            open_dt = parse_iso(g(ev, "openDate", "open_date", "startTime"))
            if open_dt is not None:
                if (now_utc - open_dt).total_seconds() > EVENT_PAST_CUTOFF:
                    continue
            if now_e - event_fetched.get(eid, 0) < EVENT_REFETCH_SECONDS:
                continue

            event_meta[eid] = (ev, comp_meta.get(cid, {}), open_dt)
            label = "markets " + str(g(ev, "name", default=eid))
            market_tasks.append((eid, "/betfair/market-all-list/" + eid, label))

    markets_by_event = fetch_many(market_tasks)
    added = 0

    for eid in markets_by_event:
        event_fetched[eid] = now_e
        raw = markets_by_event[eid]
        if not raw:
            continue
        dump_once("market-all-list", raw)
        meta = event_meta[eid]
        ev = meta[0]
        comp = meta[1]
        open_dt = meta[2]
        races, skipped = pick_race_markets(as_list(raw, "markets", "result", "data"), open_dt)
        for s in skipped:
            _skipped_names.add(s)
        for race in races:
            if register(race, ev, comp):
                added += 1

    prune_state()

    line = "DISCOVER: competitions=" + str(len(comps))
    line += " regions=" + str(sorted(regions))
    line += " events=" + str(total_events)
    line += " skipped_events=" + str(skipped_events)
    line += " markets_fetched=" + str(len(market_tasks))
    line += " new_races=" + str(added)
    line += " registry=" + str(len(registry))
    log(line)

    if _skipped_names:
        if "skipped" not in _dumps_done:
            _dumps_done.add("skipped")
            log("NON-WIN MARKET NAMES EXCLUDED: " + str(sorted(_skipped_names)[:30]))


# ---------------- alerting ----------------
def fire_alert(info, key, name, status, price, side, prev, official, rf_used,
               remaining, mkt_matched, unverified, implied, mismatch):
    now_e = time.time()

    prev_epoch = now_e
    prev_ts = ""
    prev_lay = None
    hist = []
    if prev:
        prev_epoch = prev.get("epoch", now_e)
        prev_ts = str(prev.get("ts", ""))
        prev_lay = prev.get("lay")
        hist = prev.get("hist", [])

    age = int((now_e - prev_epoch) / 60)
    if age < 0:
        age = 0
    to_off = int((info["race_epoch"] - now_e) / 60)

    is_catchup = age > STALE_MINUTES
    if is_catchup:
        STATS["catchup"] += 1
        header = "*NON-RUNNER (CATCH-UP)*"
        stale_line = "STALE: price is " + str(age) + " min old, market has likely repriced\n"
    else:
        header = "*NON-RUNNER DETECTED*"
        stale_line = ""

    if official is not None:
        STATS["official_rf"] += 1
        rf_line = "Reduction Factor: `" + pct(official) + "` (official)\n"
    else:
        STATS["derived_rf"] += 1
        rf_line = "Reduction Factor: `~" + pct(rf_used) + "` (derived)\n"

    warn_line = ""
    if unverified:
        warn_line = "Official RF unavailable - figures derived from price\n"

    mismatch_line = ""
    if mismatch:
        STATS["price_mismatch"] += 1
        mismatch_line = "PRICE MISMATCH: shown price is placeholder, RF implies `"
        mismatch_line += fmt(implied) + "`\n"

    trend = ""
    if len(hist) >= 2:
        moves = []
        for h in hist:
            moves.append(str(h.get("p")))
        trend = "Move: `" + " -> ".join(moves) + "`\n"

    lay_line = ""
    if prev_lay and side == "back":
        lay_line = "Lay: `" + fmt(prev_lay) + "`\n"

    ctry = ""
    if info.get("country"):
        ctry = " [" + info["country"] + "]"

    side_label = "Back Price"
    if side == "lay":
        side_label = "Lay Price (no back seen)"
    elif side == "implied":
        side_label = "RF-implied Price (no price seen)"

    runners_line = ""
    if remaining is not None:
        runners_line = "Runners left: " + str(remaining) + "\n"

    matched_line = ""
    if mkt_matched:
        matched_line = "Market matched: " + "{:,.0f}".format(mkt_matched) + "\n"

    msg = header + "\n\n"
    msg += "Horse: " + name + "\n"
    msg += "Race: " + info["race_label"] + ctry + "\n"
    msg += "Market: " + info["market_name"] + "\n"
    msg += "Status: `ACTIVE -> " + status + "`\n"
    msg += side_label + ": `" + fmt(price) + "`\n"
    msg += lay_line
    msg += trend
    msg += rf_line
    msg += mismatch_line
    msg += warn_line
    msg += stale_line
    msg += runners_line
    msg += matched_line
    msg += "Off in: ~" + str(to_off) + " min\n"
    msg += "Price from: `" + prev_ts + " UTC` (" + str(age) + "m ago)\n"
    msg += "Race Time: " + info["race_time"]

    rf_note = "derived " + pct(rf_used)
    if official is not None:
        rf_note = "official " + pct(official)

    logline = "ALERT: " + name + " @ " + info["race_label"]
    logline += " [ACTIVE -> " + status + "] " + side + "=" + fmt(price)
    logline += " rf=" + rf_note
    logline += " runners=" + str(remaining)
    logline += " age=" + str(age) + "m"
    logline += " off_in=" + str(to_off) + "m"
    if mismatch:
        logline += " MISMATCH"
    if is_catchup:
        logline += " CATCHUP"
    log(logline)

    if send_telegram(msg):
        alerted[key] = now_e
        save_state()
        return 1
    return 0


def evaluate_transition(info, key, name, status, back, prev, remaining, mkt_matched):
    now_e = time.time()

    # ---- 1. lead-time gate, cheapest check, before any API call ----
    country = str(info.get("country", "")).upper()
    limit = LEAD_LIMITS.get(country)
    if limit is not None:
        to_off = info["race_epoch"] - now_e
        if to_off > limit:
            alerted[key] = now_e
            STATS["filtered_lead_time"] += 1
            msg = "FILTERED: " + name + " @ " + info["race_label"]
            msg += " - " + country + " race starts in "
            msg += "{:.1f}".format(to_off / 3600.0) + "h, limit "
            msg += "{:.1f}".format(limit / 3600.0) + "h"
            log(msg)
            return 0

    # ---- 2. observed price, may be missing ----
    price = back
    side = "back"
    if not price:
        if prev:
            price = prev.get("back")
    if not price and LAY_FALLBACK:
        if prev:
            price = prev.get("lay")
            side = "lay"
    if price:
        price = float(price)
    else:
        price = None

    # ---- 3. official RF ----
    parts = key.split(":", 1)
    mid = parts[0]
    sid = parts[1]
    official = fetch_official_rf(mid, sid)

    unverified = False

    if official is not None:
        # OFFICIAL RF DECIDES. The displayed price is information only -
        # a placeholder ladder can no longer veto a large reduction factor.
        rf_used = official
        rf_src = "official"
    else:
        # FALLBACK: no official RF, so the price rules apply instead.
        unverified = True
        rf_src = "derived"
        if price is None:
            alerted[key] = now_e
            STATS["filtered_no_signal"] += 1
            log("FILTERED: " + name + " @ " + info["race_label"] + " - no price and no official RF")
            return 0
        if price >= MAX_ODDS:
            alerted[key] = now_e
            STATS["filtered_odds"] += 1
            msg = "FILTERED: " + name + " @ " + info["race_label"]
            msg += " - no RF, " + side + " " + fmt(price) + " not < " + str(MAX_ODDS)
            log(msg)
            return 0
        if price < MIN_ODDS:
            if not MIN_ODDS_ALERT_ON_UNKNOWN_RF:
                alerted[key] = now_e
                STATS["filtered_min_odds"] += 1
                msg = "FILTERED: " + name + " @ " + info["race_label"]
                msg += " - no RF and " + fmt(price) + " below " + str(MIN_ODDS)
                log(msg)
                return 0
            log("RF UNAVAILABLE: " + name + " - sub-MIN_ODDS, alerting unverified")
        rf_used = (1.0 / price) * 100.0

    # ---- 4. RF gate, identical for both paths ----
    if rf_used < RF_FLOOR:
        alerted[key] = now_e
        STATS["filtered_rf_floor"] += 1
        msg = "FILTERED: " + name + " @ " + info["race_label"]
        msg += " - RF " + pct(rf_used) + " (" + rf_src + ") below floor " + str(RF_FLOOR) + "%"
        log(msg)
        return 0

    if rf_used < RF_ALWAYS:
        if remaining is not None and remaining > RF_MID_MAX_RUNNERS:
            alerted[key] = now_e
            STATS["filtered_rf_field"] += 1
            msg = "FILTERED: " + name + " @ " + info["race_label"]
            msg += " - RF " + pct(rf_used) + " (" + rf_src + ") mid band but "
            msg += str(remaining) + " runners left, max " + str(RF_MID_MAX_RUNNERS)
            log(msg)
            return 0

    # ---- 5. price for display, plus mismatch detection ----
    implied = 0.0
    if rf_used > 0:
        implied = 100.0 / rf_used

    mismatch = False
    if price is None:
        price = implied
        side = "implied"
    elif official is not None and price > 0 and implied > 0:
        ratio = implied / price
        if ratio > PRICE_MISMATCH_RATIO:
            mismatch = True
        elif ratio < (1.0 / PRICE_MISMATCH_RATIO):
            mismatch = True

    return fire_alert(info, key, name, status, price, side, prev, official,
                      rf_used, remaining, mkt_matched, unverified, implied, mismatch)


# ---------------- process one market ----------------
def process_market(mid, info, odds_raw):
    if odds_raw is None:
        market_fail[mid] = market_fail.get(mid, 0) + 1
        n = market_fail[mid]
        if n == FAIL_ALERT_AFTER and mid not in fail_warned:
            fail_warned.add(mid)
            log("MARKET FAILING: " + info["race_label"] + " " + mid + " - " + str(n) + " failures")
            msg = "*Market not responding*\n" + info["race_label"]
            msg += "\n`" + mid + "` - " + str(n) + " consecutive failures, currently unwatched."
            send_telegram(msg)
        return 0
    market_fail[mid] = 0

    dump_once("market-odds", odds_raw)
    book = unwrap(odds_raw)

    mkt_status = str(g(book, "status", default="") or "").upper()
    if mkt_status:
        _mkt_statuses_seen[mkt_status] = _mkt_statuses_seen.get(mkt_status, 0) + 1
    if mkt_status in DEAD_MARKET_STATUSES:
        registry.pop(mid, None)
        return 0

    n_winners = as_int(g(book, "numberOfWinners"))
    if n_winners is not None and n_winners > 1:
        STATS["place_dropped"] += 1
        log("PLACE MARKET DROPPED: " + info["race_label"] + " " + info["market_name"])
        registry.pop(mid, None)
        return 0

    runners = as_list(g(book, "runners", default=None), "runners")
    if not runners:
        return 0

    n_total = as_int(g(book, "numberOfRunners"))
    n_active = as_int(g(book, "numberOfActiveRunners"))
    try:
        mkt_matched = float(g(book, "totalMatched", "total_matched", default=0) or 0)
    except (TypeError, ValueError):
        mkt_matched = 0.0

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

        name = info["runners"].get(sid)
        if not name:
            name = str(g(r, "runnerName", "name", default="Selection " + sid))

        status = str(g(r, "status", default="") or "").upper().strip()
        if status:
            _statuses_seen[status] = _statuses_seen.get(status, 0) + 1
        if status == "ACTIVE":
            listed_active += 1

        back = first_price(r, "back")
        lay = first_price(r, "lay")
        prev = runner_state.get(key)

        if status == "ACTIVE":
            hist = []
            if prev:
                hist = prev.get("hist", [])
            if back:
                hist = hist + [{"t": now_utc.strftime("%H:%M"), "p": round(back, 2)}]
            keep_back = back
            if not keep_back and prev:
                keep_back = prev.get("back")
            keep_lay = lay
            if not keep_lay and prev:
                keep_lay = prev.get("lay")
            fresh = {}
            fresh["status"] = status
            fresh["back"] = keep_back
            fresh["lay"] = keep_lay
            fresh["ts"] = now_utc.strftime("%d-%b %H:%M:%S")
            fresh["epoch"] = now_e
            fresh["hist"] = hist[-5:]
            runner_state[key] = fresh
            continue

        if prev is None:
            fresh = {}
            fresh["status"] = status
            fresh["back"] = back
            fresh["lay"] = lay
            fresh["ts"] = now_utc.strftime("%d-%b %H:%M:%S")
            fresh["epoch"] = now_e
            fresh["hist"] = []
            runner_state[key] = fresh
            continue

        was = prev.get("status")
        # snapshot BEFORE mutating, or the age shown in the alert is always 0
        prev_snapshot = dict(prev)
        runner_state[key]["status"] = status
        runner_state[key]["epoch"] = now_e
        if was != "ACTIVE":
            continue
        if key in alerted:
            continue
        alerts += evaluate_transition(info, key, name, status, back,
                                      prev_snapshot, n_active, mkt_matched)

    listed = len(listed_ids)

    if n_total is not None and listed != n_total:
        if mid not in recon_warned:
            recon_warned.add(mid)
            STATS["recon_mismatch"] += 1
            msg = "RECON MISMATCH: " + info["race_label"] + " " + mid
            msg += " - payload lists " + str(listed) + " but numberOfRunners=" + str(n_total)
            log(msg)

    if n_active is not None and listed_active != n_active:
        if mid not in recon_warned:
            recon_warned.add(mid)
            STATS["recon_mismatch"] += 1
            msg = "RECON MISMATCH: " + info["race_label"] + " " + mid
            msg += " - " + str(listed_active) + " ACTIVE listed but numberOfActiveRunners="
            msg += str(n_active)
            log(msg)

    if n_total is not None and listed == n_total:
        for sid in info["runners"]:
            if sid in listed_ids:
                continue
            key = mid + ":" + sid
            prev = runner_state.get(key)
            if not prev:
                continue
            if prev.get("status") != "ACTIVE":
                continue
            if key in alerted:
                continue
            name = info["runners"][sid]
            STATS["vanished"] += 1
            log("VANISHED: " + name + " @ " + info["race_label"] + " - no longer listed")
            prev_snapshot = dict(prev)
            runner_state[key]["status"] = "VANISHED"
            runner_state[key]["epoch"] = now_e
            alerts += evaluate_transition(info, key, name, "VANISHED", None,
                                          prev_snapshot, n_active, mkt_matched)

    return alerts


def poll_cycle():
    global _cycle_count
    now_e = time.time()
    if now_e < _rate_limited_until:
        return

    due = []
    for mid in registry:
        info = registry[mid]
        if info["race_epoch"] - now_e < -HARD_STOP_AFTER_OFF:
            continue
        if now_e - info["last_poll"] < POLL_SECONDS:
            continue
        due.append(mid)
    if not due:
        return

    for mid in due:
        registry[mid]["last_poll"] = now_e

    tasks = []
    for mid in due:
        tasks.append((mid, "/racing/market-odds/" + mid, "odds " + mid))
    results = fetch_many(tasks)

    alerts = 0
    for mid in results:
        info = registry.get(mid)
        if info:
            alerts += process_market(mid, info, results[mid])

    expired = []
    for mid in registry:
        if now_e > registry[mid]["race_epoch"] + HARD_STOP_AFTER_OFF:
            expired.append(mid)
    for mid in expired:
        registry.pop(mid, None)

    _cycle_count += 1
    if _cycle_count % STATE_SAVE_EVERY == 0:
        save_state()

    failing = 0
    for n in market_fail.values():
        if n >= FAIL_ALERT_AFTER:
            failing += 1

    line = "CYCLE: polled=" + str(len(due))
    line += " registry=" + str(len(registry))
    line += " tracked=" + str(len(runner_state))
    line += " alerts=" + str(alerts)
    line += " catchup=" + str(STATS["catchup"])
    line += " mismatch=" + str(STATS["price_mismatch"])
    line += " failing=" + str(failing)
    line += " lead_time=" + str(STATS["filtered_lead_time"])
    line += " rf_floor=" + str(STATS["filtered_rf_floor"])
    line += " rf_field=" + str(STATS["filtered_rf_field"])
    line += " no_signal=" + str(STATS["filtered_no_signal"])
    line += " min_odds=" + str(STATS["filtered_min_odds"])
    line += " max_odds=" + str(STATS["filtered_odds"])
    line += " place_dropped=" + str(STATS["place_dropped"])
    line += " recon=" + str(STATS["recon_mismatch"])
    line += " vanished=" + str(STATS["vanished"])
    line += " rf_official=" + str(STATS["official_rf"])
    line += " rf_derived=" + str(STATS["derived_rf"])
    line += " statuses=" + str(dict(sorted(_statuses_seen.items())))
    line += " mkt_statuses=" + str(dict(sorted(_mkt_statuses_seen.items())))
    line += " api_calls=" + str(STATS["calls"])
    line += " api_errors=" + str(STATS["errors"])
    log(line)


def startup_banner():
    log("=== NON-RUNNER MONITOR - PRODUCTION (DigitalOcean) ===")
    log("Base: " + API_BASE + "  alt " + API_BASE_ALT)
    if API_KEY:
        log("Auth header: sent")
    else:
        log("Auth header: NOT sent (no key)")
    if ALLOWED_REGIONS:
        log("Regions: " + str(sorted(ALLOWED_REGIONS)))
    else:
        log("Regions: ALL")
    if MAX_DAYS_AHEAD > 0:
        log("Days ahead: " + str(MAX_DAYS_AHEAD))
    else:
        log("Days ahead: unlimited")
    if LEAD_LIMITS:
        parts = []
        for k in sorted(LEAD_LIMITS):
            hours = LEAD_LIMITS[k] / 3600.0
            parts.append(k + " within " + "{:.1f}".format(hours) + "h")
        log("Lead-time limits: " + ", ".join(parts))
    else:
        log("Lead-time limits: none")
    log("Trigger: runner ACTIVE -> ANY other status, or vanished")
    log("PRIMARY GATE - official RF decides alone:")
    log("  RF >= " + str(RF_ALWAYS) + "% -> alert, any field size, any price")
    log("  RF " + str(RF_FLOOR) + "-" + str(RF_ALWAYS) + "% -> alert only if <= " + str(RF_MID_MAX_RUNNERS) + " runners left")
    log("  RF < " + str(RF_FLOOR) + "% -> never")
    log("FALLBACK when RF lookup fails: " + str(MIN_ODDS) + " <= price < " + str(MAX_ODDS))
    log("Stale threshold: alerts older than " + str(STALE_MINUTES) + " min marked CATCH-UP")
    log("RF lookup attempts: " + str(RF_ATTEMPTS))
    log("Polling: " + str(POLL_SECONDS) + "s per market, " + str(WORKERS) + " workers, tick " + str(TICK) + "s")
    log("State file: " + STATE_FILE)


def startup_telegram(had_state):
    state_note = "(cold start)"
    if had_state:
        state_note = "(state restored)"
    msg = "Non-runner monitor LIVE " + state_note + "\n"
    msg += "Host: DigitalOcean 139.59.20.81\n"
    msg += str(len(registry)) + " races registered, "
    msg += str(len(runner_state)) + " runners tracked.\n"
    msg += "RF gate: >=" + str(RF_ALWAYS) + "% any | "
    msg += str(RF_FLOOR) + "-" + str(RF_ALWAYS) + "% if <= "
    msg += str(RF_MID_MAX_RUNNERS) + " runners"
    if LEAD_LIMITS:
        msg += "\nLead limit: " + LEAD_LIMITS_RAW
    send_telegram(msg)


def main():
    startup_banner()
    load_state()
    choose_base()

    log("Initial discovery...")
    try:
        discover()
    except Exception as e:
        err("Initial discovery failed: " + str(e), e)

    had_state = len(runner_state) > 0
    if had_state:
        log("Reconciling against restored state...")
    else:
        log("Baseline sweep...")

    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            poll_cycle()
        except Exception as e:
            err("Baseline error: " + str(e), e)
        if registry:
            all_polled = True
            for v in registry.values():
                if time.time() - v["last_poll"] >= POLL_SECONDS:
                    all_polled = False
                    break
            if all_polled:
                break
        time.sleep(TICK)
    save_state()

    startup_telegram(had_state)
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


if __name__ == "__main__":
    main()
