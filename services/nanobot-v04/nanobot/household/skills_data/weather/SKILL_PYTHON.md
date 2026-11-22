```python
import subprocess, urllib.parse, json, os

# --- where "the weather" means when nobody named a place -----------------------
#
# Both functions used to demand `location`, and the skill's own examples all say
# `London`, so the model had no way to ask for the weather *here*. Measured
# 2026-09-10: gemma4:e4b answered "tell me which city" and gemma4:12b called
# `weather:get_forecast` with no arguments. Neither was wrong; there was nothing
# to pass.
#
# Order: the person's own last known fix, then the household's saved home, then
# the configured site. The fix leads because a weather question follows the
# person -- away for a week, the useful forecast is where they are, not where
# the house is. It never wakes a phone (`location` reads the stored fix) and a
# fix older than a day is ignored, since a stale one answers for a city they
# have already left.
_GEO = os.environ.get("TASKS_API_URL", "https://hub.home:21001/tasks/api").replace("/tasks/api", "/geo/api")
_USER_ID = os.environ.get("HOMECORE_USER_ID", "")
_TOKEN = os.environ.get("HOMECORE_PROXY_TOKEN", "")
_MAX_FIX_AGE_S = 86400

def _geo(path):
    if not _USER_ID or not _TOKEN:
        return None
    try:
        r = subprocess.run(
            ["curl", "-sk", "--max-time", "6", "-H", f"X-Proxy-Secret: {_TOKEN}",
             "-H", f"X-Proxy-User: {_USER_ID}", f"{_GEO}/{path}"],
            capture_output=True, text=True, timeout=8)
        return json.loads(r.stdout)
    except Exception:
        return None

def _coords(d):
    try:
        return f"{float(d['lat']):.4f},{float(d['lng']):.4f}"
    except (TypeError, KeyError, ValueError):
        return None

def house_location():
    """Where to ask about, as wttr.in takes it. "" when nothing knows."""
    fix = _geo("location")
    if isinstance(fix, dict) and fix.get("found") is not False:
        try: age = float(fix.get("age_s") or 0)
        except (TypeError, ValueError): age = 0
        here = _coords(fix)
        if here and age <= _MAX_FIX_AGE_S:
            return here
    places = _geo("places")
    rows = places.get("places") if isinstance(places, dict) else places
    for row in (rows or []):
        if str(row.get("name", "")).strip().lower() in ("home", "casa"):
            here = _coords(row)
            if here:
                return here
    # Last resort. SITE_LOCATION is the household's timezone, whose last
    # segment is a city on every tz this stack can be set to, and wttr.in takes
    # a city. A plain place name set by hand works too and is used as given.
    site = os.environ.get("SITE_LOCATION", "").strip()
    if "/" in site:
        site = site.rsplit("/", 1)[-1].replace("_", " ")
    return "" if site.upper() in ("", "UTC") else site

def _resolve(location):
    if location:
        return str(location)
    here = house_location()
    if not here:
        raise ValueError(
            "No location given and none could be worked out: no recent position, "
            "no saved 'home' place, and SITE_LOCATION is unset. Ask which city.")
    return here

def get_weather(location=None, units="metric", **kw):
    loc = urllib.parse.quote(_resolve(location).replace(" ", "+"))
    flag = "m" if units != "imperial" else "u"
    r = subprocess.run(
        ["curl", "-s", f"wttr.in/{loc}?format=3&{flag}"],
        capture_output=True, text=True, timeout=15
    )
    return r.stdout.strip() or "unavailable"

def get_forecast(location=None, days=3, units="metric", **kw):
    loc = urllib.parse.quote(_resolve(location).replace(" ", "+"))
    flag = "m" if units != "imperial" else "u"
    d = min(int(days), 3)
    r = subprocess.run(
        ["curl", "-s", f"wttr.in/{loc}?{d}T&{flag}"],
        capture_output=True, text=True, timeout=15
    )
    return r.stdout.strip() or "unavailable"

get_current_weather = get_weather
get_conditions = get_weather
```
