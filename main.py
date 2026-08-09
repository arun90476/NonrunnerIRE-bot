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
if REGIONS.strip().upper() == "ALL":
    ALLOWED_REGIONS = set()
else:
    ALLOWED_REGIONS = set()
    for _r in REGIONS.split(","):
        _r = _r.strip().upper()
        if _r:
            ALLOWED_REGIONS.add(_r)

# ---- RF / field-size gate ----
#   RF >= RF_ALWAYS            -> alert, any field size
#   RF_FLOOR <= RF < RF_ALWAYS -> alert only if remaining <= RF_MID_MAX_RUNNERS
#   RF <  RF_FLOOR             -> never alert
RF_ALWAYS = float(os.environ.get("RF_ALWAYS", "20.0"))
RF_FLOOR = float(os.environ.get("RF_FLOOR", "16.0"))
RF_MID_MAX_RUNNERS = int(os.environ.get("RF_MID_MAX_RUNNERS", "8"))

# Per-country lead-time limit: alert only if the race starts within N hours.
# Format "US:2" or "US:2,AU:3". Countries not listed have no limit.
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

MAX_ODDS = float(os.environ.get("MAX_ODDS", "6.0"))
MIN_ODDS = float(os.environ.get("MIN_ODDS", "1.20"))
GENUINE_FAV_RF = float(os.environ.get("GENUINE_FAV_RF", "50.0"))
MIN_ODDS_ALERT_ON_UNKNOWN_RF = os.environ.get(
    "MIN_ODDS_ALERT_ON_UNKNOWN_RF", "1") == "1"
RF_ATTEMPTS = int(os.environ.get("RF_ATTEMPTS", "2"))

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
    "filtered_no_price": 0,
    "filtered_odds": 0,
    "filtered_min_odds": 0,
    "filtered_lead_time": 0,
    "filtered_rf_floor": 0,
    "filtered_rf_field": 0,
    "place_dropped": 0,
    "official_rf": 0,
    "derived_rf": 0,
    "rf_unknown": 0,
    "min_odds_override": 0,
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
    body = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:4000],
        "parse_mode": "Markdown",
    }
    payload = json.dumps(body).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=15)
            code = resp.status
            resp.close()
            if code == 200:
                return True
        except Exception as e:
            err("Telegram error " + str(attempt + 1) + "/3: " + str(e))
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
    data = json.dumps(body).encode("utf-8")
    h = _headers()
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(_active_base + path, data=data,
                                 headers=h, method="POST")
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
    for key, path, label in tasks:
        futures[ex.submit(api_get, path, label)] = key
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


# -------- official Betfair reduction factor (retried) --------
def _rf_once(mid, sid):
    raw = api_post("/racing/market-listMarketBook", {"marketIds": [mid]},
                   "listMarketBook " + mid)
    books = raw.get("data") if isinstance(raw, dict) else raw
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
    """Retried, because a failed lookup would otherwise suppress the
    single most valuable alert: a genuine odds-on favourite withdrawn."""
    if not OFFICIAL_RF:
        return None
    attempts = RF_ATTEMPTS
    if attempts < 1:
        attempts = 1
    for attempt in range(attempts):
        af = None
        try:
            af = _rf_once(mid, sid)
        except Exception as e:
            if attempt + 1 >= attempts:
                err("RF lookup failed after " + str(attempts)
                    + " attempts " + mid + ":" + sid + " - " + str(e))
        if af is not None:
            return af
        if attempt + 1 < attempts:
            time.sleep(1.0)
    return None


# ---------------- state persistence ----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        log("No state file - cold start (first run will baseline).")
        return
    try:
        f = open(STATE_FILE)
        d = json.load(f)
        f.close()
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
        log("State restored: " + str(len(alerted)) + " alerted, "
            + str(kept) + " runner statuses - transitions during "
            + "downtime will still alert.")
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
    for k, v in runner_state.items():
        if now - float(v.get("epoch", now)) > RUNNER_STATE_TTL:
            stale.append(k)
    for k in stale:
        runner_state.pop(k, None)
    old = []
    for k, t in alerted.items():
        if now - float(t) > SEEN_TTL_SECONDS:
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
            rname = str(g(r, "runnerName", "runner_name", "name",
                          default="Selection " + str(sid)))
            meta = g(r, "metadata", default={}) or {}
            cloth = str(g(meta, "CLOTH_NUMBER", default="") or "").strip()
            if cloth and not rname.lstrip().startswith(cloth):
                rname = cloth + " " + rname
            runners[str(sid)] = rname

        if mstart is not None:
            key = mstart.isoformat()
        else:
            key = str(mid)
        cand = {
            "market_id": str(mid),
            "market_name": mname,
            "runners": runners,
            "start": mstart,
        }
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
    registry[mid] = {
        "event_id": str(g(event, "id", "eventId", default="")),
        "race_label": race_label,
        "market_name": market["market_name"],
        "venue": venue,
        "country": country.upper(),
        "race_epoch": race_epoch,
        "race_time": race_time,
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
        if ALLOWED_REGIONS and region.upper() not in ALLOWED_REGIONS:
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
