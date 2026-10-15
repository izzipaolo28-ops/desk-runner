# whynot.py
"""Why isn't it trading? Counts the reasons behind the coins the bot looked at and did NOT buy.

Reads the checks the sniper already keeps (state.data["checks"], newest first). Numbers are blanked so "top 10 wallets
52%" and "top 10 wallets 48%" count as one reason. Shown in /api/status as "why_not_trading".
"""
import re
from collections import Counter


def _clean(text):
    t = re.sub(r"[-+]?\$?\d[\d,]*\.?\d*[%xXkKmMbB]?", "#", str(text or "")).strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t[:80]


def reason(rep):
    """The one reason a coin wasn't bought, or None if it was (or is being) bought."""
    if rep.get("verdict") == "BUY":
        act = str(rep.get("action") or "")
        if "not bought" in act:
            return _clean(act.split("not bought:", 1)[-1])
        return None
    for key in ("veto", "wait"):
        v = rep.get(key) or []
        if v:
            return _clean(v[0])
    act = str(rep.get("action") or "")
    return _clean(act.split(":", 1)[-1]) if act else "no reason recorded"


def tally(checks, top=8):
    counts, n, bought = Counter(), 0, 0
    for rep in checks or []:
        n += 1
        r = reason(rep)
        if r is None:
            bought += 1
        else:
            counts[r] += 1
    return {"coins_checked": n, "passed": bought, "top_reasons": [{"reason": r, "coins": c} for r, c in counts.most_common(top)]}


KEEP = 1500          # entries kept in the rolling log
HOURS = 12


def record(data, rep, now):
    """Called for every coin the sniper checks: remember why it wasn't bought (or that it was). Cheap and capped."""
    r = reason(rep)
    log = data.setdefault("why_log", [])
    log.append([round(now), r])
    if len(log) > KEEP:
        del log[:len(log) - KEEP]


def tally_log(data, now, hours=HOURS, top=8):
    """The same summary as tally(), over the last `hours` hours of the rolling log."""
    cutoff = now - hours * 3600
    counts, n, passed = Counter(), 0, 0
    for ts, r in data.get("why_log") or []:
        if ts < cutoff:
            continue
        n += 1
        if r is None:
            passed += 1
        else:
            counts[r] += 1
    return {"hours": hours, "coins_checked": n, "passed": passed,
            "top_reasons": [{"reason": r, "coins": c} for r, c in counts.most_common(top)]}
