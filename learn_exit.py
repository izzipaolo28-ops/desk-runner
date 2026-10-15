# learn_exit.py
"""Learning when to cash out FULLY.

The sniper's fast plan banks profit in steps (15% / 30% / 50%) and keeps a runner. On top of that it has one "cash-out
line": a gain at which the WHOLE remaining bag is sold. That line starts at SNIPE_CASHOUT_PCT and is re-learned from the
bot's own closed trades.

For every candidate line L it asks, over the recent closed fast-plan trades: "if I had sold everything the moment the coin
touched +L%, would I have done better than what actually happened?"
    outcome(L) = (L - slip)  if the coin's best gain reached L        (sold it all there)
                 total result of the trade  otherwise                  (what really happened)
and moves the line to the candidate with the best average, only on enough trades and only when it beats the current
line by a clear margin (MIN_EDGE points a trade), so it doesn't flip back and forth. It stays between LOW and HIGH, so a lucky or unlucky streak can't push it to an extreme.

It is a rough guide, not a promise: the trades are few, the best gain is read every few seconds (a spike between two
reads is missed), and a new market behaves differently from the last 48 hours. It never touches the stop-loss, the
safety checks or the viral-hype hold.
"""
import time

CANDIDATES = (40.0, 60.0, 80.0, 100.0, 150.0)
LOW, HIGH = CANDIDATES[0], CANDIDATES[-1]
SLIP_PCT = 3.0            # a full sale at the line isn't at the line: fees and slippage


def _n(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def usable(trades, now=None, hours=48.0):
    """Closed fast-plan sniper trades with both numbers known, newest first, inside the window."""
    now = now if now is not None else time.time()
    out = []
    for t in trades or []:
        if not t.get("final") or t.get("plan") != "snipe" or t.get("viral"):
            continue
        peak, total = _n(t.get("peak_gain_pct")), _n(t.get("total_pct"))
        if peak is None or total is None:
            continue
        ts = _n(t.get("closed_ts"))
        if ts is not None and now - ts > hours * 3600:
            continue
        out.append({"peak": peak, "total": total})
    return out


def outcome(rows, line):
    return sum((line - SLIP_PCT) if r["peak"] >= line else r["total"] for r in rows) / len(rows)


def learn(trades, current, now=None, min_trades=10, min_edge=3.0, hours=48.0):
    """-> {"line": new line, "changed": bool, "n": trades used, "table": {L: average result}, "note": words}"""
    rows = usable(trades, now, hours)
    cur = min(CANDIDATES, key=lambda c: abs(c - current)) if current else 100.0
    out = {"line": float(current or cur), "changed": False, "n": len(rows), "table": {}, "note": ""}
    if len(rows) < min_trades:
        out["note"] = f"{len(rows)} of {min_trades} closed trades needed before it learns a cash-out line"
        return out
    table = {c: round(outcome(rows, c), 2) for c in CANDIDATES}
    out["table"] = table
    best = max(CANDIDATES, key=lambda c: table[c])
    if best != cur and table[best] - table[cur] >= min_edge:
        out.update(line=best, changed=True,
                   note=f"cash out the whole bag at +{best:g}% (was +{cur:g}%): over {len(rows)} trades it averaged "
                        f"{table[best]:+.1f}% a trade there against {table[cur]:+.1f}% at +{cur:g}%")
    else:
        out["note"] = f"keeping +{cur:g}% as the full cash-out line ({len(rows)} trades, {table[cur]:+.1f}% a trade)"
    return out
