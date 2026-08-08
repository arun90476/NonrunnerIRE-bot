import json
import socket
import time
import urllib.error
import urllib.request

TELEGRAM_BOT_TOKEN = "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI"
TELEGRAM_CHAT_ID = "8435489741"

TARGETS = [("primary", "157.245.44.178"),
           ("alternate", "167.99.82.136")]
PATH = "/api/betfair/competition-list/7"

lines = []


def out(msg):
    print(msg, flush=True)
    lines.append(msg)


def tcp_test(host, port=80, timeout=15):
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True, time.time() - t0, ""
    except Exception as e:
        return False, time.time() - t0, type(e).__name__ + ": " + str(e)


def http_test(url, timeout=20):
    t0 = time.time()
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        return True, time.time() - t0, "HTTP " + str(r.status) + \
            ", " + str(len(body)) + " bytes"
    except urllib.error.HTTPError as e:
        return False, time.time() - t0, "HTTP " + str(e.code)
    except Exception as e:
        return False, time.time() - t0, type(e).__name__ + ": " + str(e)


out("=== RENDER NETWORK DIAGNOSTIC ===")

# 1. does outbound internet work at all, and from which IP?
out("\n[1] Outbound internet + our public IP")
my_ip = None
for svc in ("https://api.ipify.org", "https://ifconfig.me/ip",
            "https://checkip.amazonaws.com"):
    ok, secs, info = http_test(svc, timeout=10)
    if ok:
        try:
            req = urllib.request.Request(svc, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=10) as r:
                my_ip = r.read().decode().strip()
        except Exception:
            pass
        out("  OK  " + svc + "  (" + format(secs, ".1f") + "s)")
        break
    out("  FAIL " + svc + " -> " + info)
out("  Outbound IP seen by the internet: " + str(my_ip))

# 2. raw TCP to port 80 — this is the decisive test
out("\n[2] Raw TCP connect to port 80")
for label, host in TARGETS:
    ok, secs, info = tcp_test(host)
    out("  " + label + " " + host + ":80 -> "
        + ("CONNECTED" if ok else "FAILED")
        + "  (" + format(secs, ".1f") + "s)  " + info)

# 3. the actual API call
out("\n[3] Full API request")
for label, host in TARGETS:
    url = "http://" + host + PATH
    ok, secs, info = http_test(url)
    out("  " + label + " -> " + ("OK" if ok else "FAILED")
        + "  (" + format(secs, ".1f") + "s)  " + info)

# 4. control: a plain-HTTP site, to rule out Render blocking port 80
out("\n[4] Control — plain HTTP to a public site")
ok, secs, info = http_test("http://example.com", timeout=15)
out("  example.com -> " + ("OK" if ok else "FAILED")
    + "  (" + format(secs, ".1f") + "s)  " + info)

report = "\n".join(lines)
out("\n=== END ===")

try:
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID,
                          "text": "🔍 Render network diagnostic\n```\n"
                                  + report[:3500] + "\n```",
                          "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()
except Exception as e:
    print("Telegram send failed: " + str(e), flush=True)

time.sleep(60)
