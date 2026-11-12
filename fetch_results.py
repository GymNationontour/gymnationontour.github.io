#!/usr/bin/python3
"""Pull official Emirates Dubai 7s results for GYMNATION into the Dubai 7s HQ dashboard.

The official schedule page (emiratesdubai7s.com/schedule-results) loads its data from
/umbraco/Surface/D7s/GetSchedule (POST, needs the page's session cookie + anti-forgery token).
This script fetches it, keeps only the International Vets Social comp, and writes
`window.D7S_LIVE = {...}` to live.js inside the app bundle (a <script> works from file://,
fetch() does not).

Run by LaunchAgent com.frankafeaki.dubai7s.results every 2 min; it self-throttles to
every run on match days (Dubai 07:00-20:00, 27-29 Nov) and every 3 h otherwise.
Use --force to fetch regardless. Stdlib only; must run under /usr/bin/python3 (CA certs).
"""
import http.cookiejar, json, os, re, sys, tempfile, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://www.emiratesdubai7s.com"
PAGE = BASE + "/schedule-results"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
COMP = "International Vets Social"
TEAM = "gymnation"
# Outside ~/Desktop on purpose: launchd jobs are blocked from writing there (macOS privacy).
# The page loads this absolute path first, then a bundled copy.
OUT = [os.path.expanduser("~/.claude-work/Dubai7s-Dashboard/site/live.js")]
if os.environ.get("D7S_OUT"):  # GitHub Actions build writes straight into the Pages folder
    OUT = [os.environ["D7S_OUT"]]
STAMP = os.path.expanduser("~/.claude-work/Dubai7s-Dashboard/.last_fetch")
DXB = timezone(timedelta(hours=4))


def due():
    if "--force" in sys.argv:
        return True
    now = datetime.now(DXB)
    matchday = now.date().isoformat() in ("2026-11-27", "2026-11-28", "2026-11-29") and 7 <= now.hour < 20
    try:
        age = time.time() - os.path.getmtime(STAMP)
    except OSError:
        return True
    return matchday or age > 3 * 3600


def tlist(d):
    t = d.get("tournaments") or []
    return list(t.values()) if isinstance(t, dict) else t


def fetch():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", UA)]
    html = op.open(PAGE, timeout=40).read().decode("utf-8", "replace")
    tok = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', html).group(1)
    tids = [int(x) for x in re.findall(r"API_TID\s*=\s*(\d+)", html)]

    def post(ep, tid):
        req = urllib.request.Request(BASE + "/umbraco/Surface/D7s/" + ep,
            data=urllib.parse.urlencode({"id": tid, "__RequestVerificationToken": tok}).encode(),
            headers={"X-Requested-With": "XMLHttpRequest", "RequestVerificationToken": tok, "Referer": PAGE})
        d = json.loads(op.open(req, timeout=60).read())
        return json.loads(d) if isinstance(d, str) else d

    # newest tournament id on the page (2026 = 2633 at time of writing)
    for tid in sorted(set(tids), reverse=True):
        d = post("GetSchedule", tid)
        if any("2026" in (t.get("tournamentName") or "") for t in tlist(d)):
            return d
    raise RuntimeError("no 2026 tournament found among API_TIDs %s" % tids)


def build(d):
    comp = next(t for t in tlist(d) if (t.get("tournamentName") or "").strip() == COMP)
    teams = d["teams"]
    name = lambda i: (teams.get(str(i)) or {}).get("name", "").strip() if i else ""
    logo = lambda i: (teams.get(str(i)) or {}).get("avatarThumb") if i else None
    us = next(int(k) for k, t in teams.items() if (t.get("name") or "").strip().lower() == TEAM)

    def num(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    def side(x):
        return {"id": x.get("id"), "name": name(x.get("id")) or x.get("description") or "TBC",
                "logo": logo(x.get("id")), "score": num(x.get("points") if x.get("points") is not None else x.get("score"))}

    def match(m):
        a, b = [side(x) for x in m["teams"]]
        done = a["score"] is not None and b["score"] is not None
        return {"id": m["id"], "no": m.get("matchNumberStr"), "type": m["matchType"], "stage": m["title"],
                "utc": m["startDateTimeUTC"] + "Z", "local": m["startDateTime"], "pitch": m.get("pitch"),
                "a": a, "b": b, "done": done, "winner": m.get("winner"), "postponed": m.get("postponed"),
                "state": m.get("resultState") or ""}

    ms = [match(m) for m in d["matches"] if m["tid"] == comp["tid"]]
    ours = [m for m in ms if us in (m["a"]["id"], m["b"]["id"])]
    for m in ours:  # orient so GYMNATION is always side "a"
        if m["b"]["id"] == us:
            m["a"], m["b"] = m["b"], m["a"]

    pool_b = [m for m in ms if m["stage"] == "Pool B"]
    table = {}
    for m in pool_b:
        for s in (m["a"], m["b"]):
            table.setdefault(s["id"], {"id": s["id"], "name": s["name"], "logo": s["logo"], "p": 0, "w": 0, "d": 0, "l": 0, "pf": 0, "pa": 0, "pts": 0})
        if not m["done"]:
            continue
        for s, o in ((m["a"], m["b"]), (m["b"], m["a"])):
            r = table[s["id"]]; r["p"] += 1; r["pf"] += s["score"]; r["pa"] += o["score"]
            if s["score"] > o["score"]: r["w"] += 1; r["pts"] += 3
            elif s["score"] == o["score"]: r["d"] += 1; r["pts"] += 2
            else: r["l"] += 1; r["pts"] += 1
    rows = sorted(table.values(), key=lambda r: (-r["pts"], -(r["pf"] - r["pa"]), -r["pf"], r["name"]))

    ko = [m for m in ms if m["type"] == "Knockout"]
    return {"fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"), "comp": comp["tournamentName"],
            "us": us, "ours": sorted(ours, key=lambda m: m["utc"]), "poolB": rows,
            "poolBMatches": sorted(pool_b, key=lambda m: m["utc"]), "knockouts": sorted(ko, key=lambda m: m["utc"])}


def write(payload):
    raw = json.dumps(payload, ensure_ascii=False)
    for p in OUT:
        if not os.path.isdir(os.path.dirname(p)):
            continue
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
        with os.fdopen(fd, "w") as f:
            f.write(raw if p.endswith(".json") else "window.D7S_LIVE = " + raw + ";\n")
        os.chmod(tmp, 0o644); os.replace(tmp, p)


if __name__ == "__main__":
    if not due():
        sys.exit(0)
    try:
        payload = build(fetch())
    except Exception as e:  # keep the last good live.js; the page shows its age
        print(datetime.now(DXB).isoformat(timespec="seconds"), "FETCH FAILED:", repr(e), file=sys.stderr)
        sys.exit(1)
    try:
        w = urllib.request.urlopen(urllib.request.Request("https://api.open-meteo.com/v1/forecast?latitude=25.05&longitude=55.24&current=temperature_2m,weather_code,wind_speed_10m&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=Asia%2FDubai&forecast_days=16", headers={"User-Agent": UA}), timeout=30)
        payload["weather"] = json.loads(w.read())
    except Exception as e:
        print("weather skipped:", repr(e), file=sys.stderr)
    write(payload)
    if not os.environ.get("D7S_OUT"):
        open(STAMP, "w").write(payload["fetched"])
    done = sum(m["done"] for m in payload["ours"])
    print(datetime.now(DXB).isoformat(timespec="seconds"), f"ok: {len(payload['ours'])} GYMNATION matches, {done} with scores")
