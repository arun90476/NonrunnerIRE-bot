import datetime
import html as htmllib
import json
import os
import re
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
IHRB_URL = "https://www.ihrb.ie/non-runners"

MB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}

MB_POLL_SECONDS = 15
IHRB_POLL_SECONDS = 60
PURGE_AFTER_SECONDS = 7200
SEEN_TTL_SECONDS = 172800
HISTORY_LEN = 5
STATE_FILE = os.environ.get("STATE_FILE", "nr_ire_state.json")

# --- ALERT FILTERS (same as Runner 1; all must pass) ---
MAX_ODDS = 3.5
REQUIRE_VOLUME = True
IE_TZ = ZoneInfo("Europe/Dublin")

RESERVE_EXCUSES = {"reserve declaration", "nomination of rider",
                   "winners penalty", "advised rider"}

price_cache = {}
name_index = {}
seen_official = {}


def log(msg):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def err(msg, exc=None):
    log(f"[ERR] {msg}")
    if exc is not None:
        print(traceback.format_exc(), flush=True)


def norm_name(name):
    """'1: Atlantic Gamble (GB)' / '5 Atlantic Gamble' -> 'atlantic gamble'"""
    s = str(name).strip().lower()
    s = re.sub(r"^\d+\s*[:.]?\s*", "", s)
    s = re.sub(r"\((gb|fr|ire|usa|ger|jpn|aus|nz|ity?|es|saf|can|uae)\)",
               " ", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_today_ie(event_dt_utc):
    now_ie = datetime.datetime.now(IE_TZ)
    return event_dt_utc.astimezone(IE_TZ).date() == now_ie.date()


def passes_filters(cached):
    if not cached:
        return False, "no cached price"
    mid = cached.get("mid")
    vol = cached.get("vol") or 0
    if not mid or mid <= 1.0:
        return False, "no usable price"
    if mid > MAX_ODDS:
        return False, f"odds {mid:.2f} > {MAX_ODDS}"
    if REQUIRE_VOLUME and vol <= 0:
        return False, "matched volume is 0"
    return True, ""


# ---------- persistence ----------
def load_state():
    if not os.path.exists(STATE_FILE):
        log("No state file — cold start.")
        return
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        now = time.time()
        for k, v in (d.get("prices") or {}).items():
            try:
                if now - v.get("race_epoch", 0) < PURGE_AFTER_SECONDS:
                    price_cache[int(k)] = v
            except Exception:
                continue
        raw = d.get("seen_official")
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    if now - float(v) < SEEN_TTL_SECONDS:
                        seen_official[str(k)] = float(v)
                except Exception:
                    continue
        log(f"Loaded cache={len(price_cache)} "
            f"official_seen={len(seen_official)}")
    except Exception as e:
        err(f"State load failed: {e}", e)


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"prices": price_cache,
                       "seen_official": seen_official}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        err(f"State save failed: {e}", e)


def purge_stale():
    now = time.time()
    for r in [r for r, v in price_cache.items()
              if now - v.get("race_epoch", now) > PURGE_AFTER_SECONDS]:
        v = price_cache.pop(r, None)
        if v:
            name_index.pop(norm_name(v.get("name", "")), None)
    for k in [k for k, t in seen_official.items()
              if now - t > SEEN_TTL_SECONDS]:
        seen_official.pop(k, None)


# ---------- network ----------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": TELEGRAM_CHAT_ID, "text": message,
         "parse_mode": "Markdown"}).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True
                err(f"Telegram HTTP {resp.status}")
        except Exception as e:
            err(f"Telegram error ({attempt + 1}/3): {e}")
            time.sleep(2)
    return False


def get_json(url):
    req = urllib.request.Request(url, headers=MB_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def get_text(url):
    req = urllib.request.Request(url, headers=WEB_HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


# ---------- matchbook (price cache only — NO alerting here) ----------
def is_withdrawn(runner):
    status = str(runner.get("status", "")).lower()
    wd = runner.get("withdrawn")
    if wd is True or (isinstance(wd, str) and wd.lower() == "true"):
        return True
    return status in ("withdrawn", "scratched", "removed",
                      "non-runner", "nonrunner")


def extract_book(runner):
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
    return best_back, best_lay, mid, vol


def fmt(v):
    return f"{v:.2f}" if v else "N/A"


def mb_cache_scan():
    markets = stored = 0
    try:
        events = get_json(EVENTS_URL).get("events", []) or []
    except Exception as e:
        err(f"Events fetch failed: {e}")
        return

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
        if event_dt <= now_utc or not is_today_ie(event_dt):
            continue

        event_id = event.get("id")
        event_name = event.get("name", "Unknown Race")
        race_epoch = event_dt.timestamp()
        race_time = start_str[:16].replace("T", " ") + " UTC"

        for market in event.get("markets", []) or []:
            if "win" not in str(market.get("name", "")).lower():
                continue
            market_id = market.get("id")
            url = (f"https://api.matchbook.com/edge/rest/events/{event_id}"
                   f"/markets/{market_id}/runners"
                   "?include-withdrawn=true&include-prices=true"
                   "&price-depth=3")
            try:
                runners = get_json(url).get("runners", []) or []
            except Exception as e:
                err(f"Market {market_id} fetch failed: {e}")
                continue
            if not runners:
                continue
            markets += 1

            for runner in runners:
                rid = runner.get("id")
                if not rid or is_withdrawn(runner):
                    continue
                back, lay, mid, vol = extract_book(runner)
                if not mid:
                    continue
                prev = price_cache.get(rid, {})
                hist = prev.get("history", [])
                hist.append({"t": now_utc.strftime("%H:%M"),
                             "mid": round(mid, 2)})
                rname = runner.get("name", "")
                price_cache[rid] = {
                    "back": back, "lay": lay, "mid": mid, "vol": vol,
                    "ts": now_utc.strftime("%d-%b %H:%M:%S"),
                    "epoch": now_epoch, "name": rname,
                    "race": event_name, "race_time": race_time,
                    "race_epoch": race_epoch,
                    "history": hist[-HISTORY_LEN:],
                }
                name_index[norm_name(rname)] = rid
                stored += 1

    purge_stale()
    log(f"MB cache: markets={markets} stored={stored} "
        f"cache={len(price_cache)}")


# ---------- IHRB official detection ----------
def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return htmllib.unescape(re.sub(r"\s+", " ", s)).strip()


def parse_ihrb(page_html):
    cut = re.split(r"Reserves\s+and\s+Further\s+Information",
                   page_html, maxsplit=1, flags=re.IGNORECASE)[0]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", cut,
                         re.DOTALL | re.IGNORECASE):
        tds = [strip_tags(td) for td in
               re.findall(r"<td[^>]*>(.*?)</td>", tr,
                          re.DOTALL | re.IGNORECASE)]
        if len(tds) < 4:
            continue
        track, race_no, horse_cell = tds[0], tds[1], tds[2]
        excuse = tds[3] if len(tds) > 3 else ""
        logged = tds[4] if len(tds) > 4 else ""
        if excuse.strip().lower() in RESERVE_EXCUSES:
            continue
        tm = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(.+)", track)
        if not tm:
            continue
        try:
            row_date = datetime.date(int(tm.group(3)), int(tm.group(2)),
                                     int(tm.group(1)))
        except ValueError:
            continue
        course = tm.group(4).strip()
        hm = re.match(r"(\d+)\s*:\s*(.+)", horse_cell)
        cloth = hm.group(1) if hm else ""
        horse_raw = hm.group(2).strip() if hm else horse_cell
        rows.append({"date": row_date, "course": course,
                     "race": race_no.strip(), "cloth": cloth,
                     "horse_raw": horse_raw, "excuse": excuse,
                     "logged": logged})
    return rows


def match_to_cache(horse_raw, course=""):
    """Longest cached-name prefix wins; a candidate whose cached
    Matchbook race contains the IHRB course beats one that doesn't
    (soft tiebreaker — falls back to name-only if courses never match)."""
    target = norm_name(horse_raw)
    course_key = re.sub(r"[^a-z]", "", str(course).lower())
    best = None  # (course_ok, name_len, rid)

    for nm, rid in name_index.items():
        if not nm:
            continue
        if target == nm or target.startswith(nm + " "):
            cached = price_cache.get(rid) or {}
            race_key = re.sub(r"[^a-z]", "",
                              str(cached.get("race", "")).lower())
            course_ok = bool(course_key) and course_key in race_key
            cand = (course_ok, len(nm), rid)
            if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                best = cand

    if best is None:
        return None
    if not best[0] and course_key:
        log(f"MATCH NOTE: '{horse_raw}' matched by name only — "
            f"course '{course}' not found in cached race name")
    return best[2]


def build_alert(name, race, race_time, cached, reason, logged):
    c = cached or {}
    back, lay, mid = c.get("back"), c.get("lay"), c.get("mid")
    vol = c.get("vol") or 0
    snap = c.get("ts")
    hist = c.get("history", [])

    if snap:
        age = int((time.time() - c.get("epoch", time.time())) / 60)
        snap_line = f"🕐 *Captured:* `{snap} UTC` ({age}m before scratch)\n"
    else:
        snap_line = "⚠️ _No stored price for this runner._\n"

    rf = f"~{(1 / mid) * 100:.1f}%" if mid and mid > 1.0 else "N/A"

    trend = ""
    if len(hist) >= 2:
        moves = " → ".join(str(h["mid"]) for h in hist)
        direction = ("shortening" if hist[-1]["mid"] < hist[0]["mid"]
                     else "drifting" if hist[-1]["mid"] > hist[0]["mid"]
                     else "steady")
        trend = f"📈 *Move:* `{moves}` ({direction})\n"

    reason_line = f"📋 *Reason:* {reason}\n" if reason else ""
    logged_line = f"🖊️ *Officially logged:* `{logged}`\n" if logged else ""

    return (
        f"☘️ *NON-RUNNER — IHRB OFFICIAL*\n\n"
        f"🏇 *Horse:* {name}\n"
        f"📍 *Race:* {race}\n"
        f"{reason_line}"
        f"{logged_line}"
        f"📊 *Pre-Scratch Price:* `{fmt(mid)}`\n"
        f"📘 *Back:* `{fmt(back)}` / *Lay-equiv:* `{fmt(lay)}`\n"
        f"💰 *Matched Volume:* `{vol:,.0f}`\n"
        f"{trend}"
        f"📉 *Est. Reduction Factor:* `{rf}`\n"
        f"{snap_line}"
        f"⏰ *Race Time:* {race_time}"
    )


def check_ihrb(warmup=False):
    alerts = 0
    try:
        page = get_text(IHRB_URL)
    except Exception as e:
        err(f"IHRB fetch failed: {e}")
        return 0

    rows = parse_ihrb(page)
    if not rows:
        log("IHRB: 0 rows parsed (quiet day, or page format changed)")
        return 0

    today_ie = datetime.datetime.now(IE_TZ).date()
    now_epoch = time.time()
    new_rows = todays = filtered = 0

    for r in rows:
        if r["date"] != today_ie:
            continue
        todays += 1
        key = (f"{r['date']}|{r['course']}|{r['race']}|{r['cloth']}|"
               f"{norm_name(r['horse_raw'])}")
        if key in seen_official:
            continue
        new_rows += 1

        if warmup:
            seen_official[key] = now_epoch
            continue

        rid = match_to_cache(r["horse_raw"], r["course"])
        cached = price_cache.get(rid) if rid else None
        display = (f"{r['cloth']} {r['horse_raw']}".strip()
                   if r["cloth"] else r["horse_raw"])
        race_label = f"Race {r['race']}, {r['course']}"

        ok, why = passes_filters(cached)
        if not ok:
            seen_official[key] = now_epoch
            filtered += 1
            log(f"IHRB FILTERED: {display} @ {race_label} — {why} "
                f"(reason: {r['excuse']})")
            continue

        msg = build_alert(display, race_label,
                          cached.get("race_time", "today"), cached,
                          r["excuse"], r["logged"])
        log(f"IHRB ALERT: {display} @ {race_label} "
            f"mid={cached['mid']:.2f} reason={r['excuse']}")
        if send_telegram(msg):
            seen_official[key] = now_epoch
            alerts += 1
            save_state()

    log(f"IHRB: rows={len(rows)} today={todays} new={new_rows} "
        f"filtered={filtered} alerts={alerts}")
    return alerts


if __name__ == "__main__":
    log("=== RUNNER 2 — IRELAND (IHRB OFFICIAL) STARTING ===")
    log(f"Filters: today's Irish card | odds <= {MAX_ODDS} | volume > 0")
    log(f"IHRB poll {IHRB_POLL_SECONDS}s | MB price cache "
        f"{MB_POLL_SECONDS}s | UK handled by Runner 1")
    load_state()

    log("Warm-up: matchbook price cache...")
    try:
        mb_cache_scan()
    except Exception as e:
        err(f"Warm-up MB failed: {e}", e)

    log("Warm-up: marking existing IHRB rows as seen...")
    try:
        check_ihrb(warmup=True)
    except Exception as e:
        err(f"Warm-up IHRB failed: {e}", e)

    save_state()
    log(f"Warm-up done. Cache={len(price_cache)} "
        f"seen={len(seen_official)}. Alerting live.")

    last_ihrb = 0.0
    cycle = 0
    while True:
        try:
            mb_cache_scan()
            if time.time() - last_ihrb >= IHRB_POLL_SECONDS:
                last_ihrb = time.time()
                check_ihrb(warmup=False)
            cycle += 1
            if cycle % 20 == 0:
                save_state()
        except KeyboardInterrupt:
            save_state()
            log("Stopped.")
            break
        except Exception as e:
            err(f"Loop error: {e}", e)
        time.sleep(MB_POLL_SECONDS)
