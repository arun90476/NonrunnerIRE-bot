import json, urllib.request
from getpass import getpass

KEY = getpass("SportBex API key: ")
BASE = "https://trial-api.sportbex.com/api"

def get(path):
    req = urllib.request.Request(BASE + path, headers={
        "sportbex-api-key": KEY, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

try:
    comps = get("/betfair/competition-list/7")
    print("REACHABLE ✓ — competitions:", json.dumps(comps, indent=1)[:800])
except Exception as e:
    print("BLOCKED or failed:", e)
