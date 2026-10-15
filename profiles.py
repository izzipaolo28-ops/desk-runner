# profiles.py
"""Speed profiles: how often the sniper looks and how fast WATCH reads prices.

SPEED_PROFILE picks one of the sets below. A profile only fills in variables you have NOT set yourself, so
`SNIPE_SCAN_SECONDS=45` in Railway still wins over the profile. The default is "balanced".

  normal   the original pace: a look every 60 s, a second look 60 s later, 2 full checks a round, prices every 10 s
  balanced a look every 45 s, a second look 30 s later, 2 full checks a round, prices every 5 s   (default)
  fast     a look every 30 s, a second look 30 s later, 3 full checks a round, prices every 5 s
  turbo    a look every 20 s, a second look 15 s later, 4 full checks a round, prices every 5 s

Faster means more calls to GeckoTerminal, DexScreener, RugCheck and your RPC. If the feed starts saying
"too many requests" (429), or your Helius plan runs dry, go down one step: SPEED_PROFILE=normal.
The safety checks are the same at every speed: a faster look never skips a rug check.
"""
import os

PROFILES = {
    "normal": {},
    "balanced": {
        "SNIPE_SCAN_SECONDS": "45",
        "SNIPE_CONFIRM_SECONDS": "30",
        "SNIPE_CHECKS_PER_ROUND": "2",
        "WATCH_SECONDS": "5",
    },
    "fast": {
        "SNIPE_SCAN_SECONDS": "30",
        "SNIPE_CONFIRM_SECONDS": "30",
        "SNIPE_CHECKS_PER_ROUND": "3",
        "WATCH_SECONDS": "5",
    },
    "turbo": {
        "SNIPE_SCAN_SECONDS": "20",
        "SNIPE_CONFIRM_SECONDS": "15",
        "SNIPE_CHECKS_PER_ROUND": "4",
        "WATCH_SECONDS": "5",
    },
}
DEFAULT_PROFILE = "balanced"


def profile_name(env=os.environ):
    name = str(env.get("SPEED_PROFILE", "")).strip().lower() or DEFAULT_PROFILE
    if name not in PROFILES:
        raise ValueError("SPEED_PROFILE must be normal, balanced, fast or turbo")
    return name


def with_profile(env=os.environ):
    """A copy of the variables with the profile's numbers filled in wherever you set none."""
    out = dict(env)
    for key, value in PROFILES[profile_name(env)].items():
        if not str(out.get(key, "")).strip():
            out[key] = value
    return out
