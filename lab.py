# lab.py
"""THE LAB: the perps bot, run on a year of real prices, and every losing trade taken apart.

It pulls hourly candles for SOL, ETH and BTC from Coinbase (public, no key; 300 a request, paged back as far as
LAB_DAYS, default 365) and builds the 4-hour ones from them, the same way the bot's own Coinbase fallback does. The
candles are cached on the volume, so a re-run only fetches what's new.

Then it runs the bot's own replay() (the same code that trades: entries, stops, fees, borrow, liquidation) over:
- the whole period with the current settings (the baseline),
- the first half and the second half apart (does it work in both, or did one lucky stretch carry it?),
- a set of variants, one change each (wider stop, longer trail, no partial, stronger trend filter...), each also on
  both halves, so a change only counts as better when it's better in both.

Every losing trade gets a cause, read from the prices during and after it:
- SHAKEOUT: the stop was hit, then the price went on to +2R the trade's way within 3 days (right idea, stop too tight)
- GAVE BACK: it was up 1R or more at some point and still ended as a loss (the exit let a winner turn into a loser)
- WRONG WAY: after the stop the price kept going against the trade by another 1R or more (wrong direction / entry)
- EARLY EXIT: closed by the trend-flip or time rule at a loss, and the price then reached +2R the trade's way
- CHOP: none of those: the market went nowhere and the fees and the stop took it

GET /api/perps-lab: the result (JSON). GET /lab: the same as plain text, for copying from a phone.
POST /api/perps-lab (admin token): run it now. It also runs by itself a few minutes after each start, then daily.
"""
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone

import collect
import perps

log = logging.getLogger("lab")

COINBASE = "https://api.exchange.coinbase.com"
H1, H4 = 3600, 14400
RISE = 2.0                      # R the price must reach after a stop to call it a shakeout / early exit
LOOK = 72 * H1                  # how long after the exit it looks

# one change each, against the current settings. (name, what it tests, {PERP_ variable: value})
VARIANTS = [
    ("stop 1.6 ATR", "a wider stop: fewer shakeouts, smaller size", {"PERP_STOP_ATR": "1.6"}),
    ("stop 2.0 ATR", "a much wider stop", {"PERP_STOP_ATR": "2.0"}),
    ("stop 0.8 ATR", "a tighter stop: bigger size, more stop-outs", {"PERP_STOP_ATR": "0.8"}),
    ("trail 2 ATR", "a tighter trail: keeps more of each winner, cuts big runs", {"PERP_TRAIL_ATR": "2"}),
    ("trail 4 ATR", "a looser trail: lets the big runs go further", {"PERP_TRAIL_ATR": "4"}),
    ("partial at 1.5R", "bank the first third sooner", {"PERP_TP1_R": "1.5", "PERP_BREAKEVEN_R": "1.5"}),
    ("partial at 3R", "bank the first third later", {"PERP_TP1_R": "3", "PERP_BREAKEVEN_R": "3"}),
    ("half at 2R", "bank half instead of a third", {"PERP_TP1_PCT": "50"}),
    ("no partial", "no early profit: everything trails (break-even still at 2R)", {"PERP_TP1_PCT": "0"}),
    ("break-even at 1R", "protect sooner", {"PERP_BREAKEVEN_R": "1"}),
    ("no break-even", "the stop stays put until the trail takes over", {"PERP_BREAKEVEN_R": "10"}),
    ("trend filter 0.6", "only stronger 4-hour trends", {"PERP_MIN_TREND": "0.6"}),
    ("trend filter 1.0", "only the strongest 4-hour trends", {"PERP_MIN_TREND": "1.0"}),
    ("trend filter 0.15", "weaker trends allowed: more trades", {"PERP_MIN_TREND": "0.15"}),
    ("deeper dip (RSI 40)", "wait for a deeper cool-off before buying", {"PERP_RSI_LONG": "40", "PERP_RSI_SHORT": "60"}),
    ("shallower dip (RSI 50)", "a lighter cool-off: more trades", {"PERP_RSI_LONG": "50", "PERP_RSI_SHORT": "50"}),
    ("dip within 0.6 ATR", "count dips that come near the 20 EMA, not only to it", {"PERP_PULL_ATR": "0.6"}),
    ("longs only", "skip the shorts", {"__side": "long"}),
    ("shorts only", "skip the longs", {"__side": "short"}),
    ("risk 3%", "a stop-out costs 3% of the balance", {"PERP_MAX_LOSS_PCT": "3"}),
    ("risk 8%", "a stop-out costs 8% of the balance", {"PERP_MAX_LOSS_PCT": "8"}),
    ("cooldown 12h", "rest 12 hours after a loss", {"PERP_COOLDOWN_MINUTES": "720"}),
]


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ---------- prices ----------
def fetch_hours(coin, start, end, pause=0.35):
    """Closed 1-hour candles from start to end (unix seconds), oldest first, paged 300 at a time."""
    out, t = {}, start
    while t < end:
        u = min(end, t + 300 * H1)
        r = collect.HTTP.get(f"{COINBASE}/products/{perps.COINS[coin]['coinbase']}/candles",
                             params={"granularity": 3600, "start": _iso(t), "end": _iso(u)}, timeout=15)
        if r.status_code == 429:
            time.sleep(2)
            continue
        if r.status_code != 200:
            raise collect.FetchFailed("coinbase", f"HTTP {r.status_code}", r.status_code)
        for row in r.json() or []:
            c = perps._candle(row[0], row[3], row[2], row[1], row[4]) if isinstance(row, list) and len(row) >= 5 else None
            if c:
                out[c["t"]] = c
        t = u
        time.sleep(pause)
    now = time.time()
    return [out[k] for k in sorted(out) if k + H1 <= now]


def four_hours(h1):
    b = {}
    for c in h1:
        b.setdefault(c["t"] - c["t"] % H4, []).append(c)
    return [{"t": t, "o": v[0]["o"], "h": max(x["h"] for x in v), "l": min(x["l"] for x in v), "c": v[-1]["c"]}
            for t, v in sorted(b.items()) if len(v) == 4]


def load(coins, days, cache_dir, say=lambda s: None):
    """{coin: [1-hour candles]} for the last `days`, from the cache plus what's new."""
    now = int(time.time())
    want = now - now % H1 - days * 86400
    data = {}
    for coin in coins:
        path = os.path.join(cache_dir, f"lab_{coin}_1h.json")
        have = []
        try:
            with open(path) as f:
                have = [c for c in json.load(f) if c["t"] >= want]
        except (OSError, ValueError, KeyError, TypeError):
            have = []
        if not have or have[0]["t"] > want + 6 * H1:
            say(f"fetching a year of {coin} 1-hour prices from Coinbase")
            try:
                have = fetch_hours(coin, want, now)
            except Exception as exc:              # Coinbase refused: Kraken's last 720 hours (30 days) instead
                say(f"Coinbase failed for {coin} ({type(exc).__name__}): using Kraken's last 30 days")
                have = perps.kraken_candles(coin, 60)[0]
        else:
            say(f"{coin}: {len(have)} cached candles, fetching the new ones")
            seen = {c["t"] for c in have}
            have += [c for c in fetch_hours(coin, have[-1]["t"] + H1, now) if c["t"] not in seen]
            have.sort(key=lambda c: c["t"])
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(have, f)
            os.replace(tmp, path)
        except OSError:
            pass
        data[coin] = have
    return data


# ---------- running the bot ----------
def _cfg(env_base, change):
    env = dict(env_base)
    env.update({k: v for k, v in change.items() if not k.startswith("__")})
    return perps.PerpConfig(env)


def run(h1data, cfg, side=None, t0=None, t1=None):
    """The bot's own replay over [t0, t1) (with the history before t0 to warm up), every trade kept."""
    data = {}
    for coin, h1 in h1data.items():
        lo = (t0 or h1[0]["t"]) - 140 * H1               # the entry chart's warm-up
        lo4 = (t0 or h1[0]["t"]) - 140 * H4              # the trend chart's
        hi = t1 or (h1[-1]["t"] + 1)
        h4 = four_hours([c for c in h1 if lo4 <= c["t"] < hi])
        ent = [c for c in h1 if lo <= c["t"] < hi]
        data[coin] = {"entry": ent, "trend": h4}
    if side:                                              # longs only / shorts only: the other side's setups never arm
        orig = perps.read_coin

        def only(coin, a, b, c, now=None):
            out = orig(coin, a, b, c, now)
            if out.get("setup") and out["setup"]["side"] != side:
                out["setup"] = None
                out["wait"] = [f"{side}s only"]
            return out
        perps.read_coin = only
        try:
            res = perps.replay(data, cfg, bank=cfg.paper_bank, full=True)
        finally:
            perps.read_coin = orig
    else:
        res = perps.replay(data, cfg, bank=cfg.paper_bank, full=True)
    trades = [x for x in res.get("all") or [] if x.get("final")]
    if t0:
        trades = [x for x in trades if (x.get("opened_ts") or 0) >= t0]
    res["final"] = trades
    return res


def stats(res):
    tr = res["final"]
    tot = [x.get("total") or 0.0 for x in tr]
    w = [v for v in tot if v > 0]
    l = [v for v in tot if v <= 0]
    gp, gl = sum(w), -sum(l)
    rr = [((x.get("total") or 0.0) / (x.get("stake") or 1)) for x in tr]
    return {"trades": len(tr), "won": len(w), "win_pct": round(100 * len(w) / len(tr)) if tr else 0,
            "start": res["start"], "end": res["end"], "ret_pct": res["pnl_pct"], "max_dd_pct": res["max_drawdown_pct"],
            "profit_factor": round(gp / gl, 2) if gl > 0 else (99.0 if gp > 0 else 0.0),
            "avg_win": round(gp / len(w), 2) if w else 0.0, "avg_loss": round(-gl / len(l), 2) if l else 0.0,
            "liquidated": res["liquidated"], "per_week": round(7 * len(tr) / res["days"], 1) if res.get("days") else 0,
            "avg_hold_h": round(sum(x.get("held_min") or 0 for x in tr) / len(tr) / 60, 1) if tr else 0,
            "best_trade": round(max(tot), 2) if tot else 0, "worst_trade": round(min(tot), 2) if tot else 0,
            "stopped": res.get("stopped")}


# ---------- the autopsy ----------
def autopsy(trades, h1data):
    """A cause for every losing trade (see the top of the file), and how the losers differ from the winners."""
    idx = {c: [x["t"] for x in v] for c, v in h1data.items()}
    rows = []
    for x in trades:
        tot = x.get("total") or 0.0
        if tot > 0:
            continue
        coin, long = x["coin"], x["side"] == "long"
        sg = 1 if long else -1
        r = x.get("r_px") or 0
        entry, stop = x["entry"], x.get("stop0") or x["entry"]
        why = str(x.get("why") or "")
        after = []
        ts = x.get("closed_ts") or 0
        h = h1data.get(coin) or []
        import bisect
        i = bisect.bisect_left(idx.get(coin, []), ts)
        after = h[i:i + int(LOOK / H1)]
        up = max(((c["h"] if long else -c["l"]) for c in after), default=None)
        reached_2r = up is not None and r and sg * ((up if long else -up) - entry) >= RISE * r
        # did it go further against before it got back to the entry?
        further = False
        if r:
            for c in after:
                if sg * ((c["l"] if long else c["h"]) - stop) <= -1.0 * r:
                    further = True
                    break
                if sg * ((c["h"] if long else c["l"]) - entry) >= 0:
                    break
        mfe = x.get("mfe_r") or 0.0
        stopped = why.startswith("stop-loss") or why.startswith("break-even") or why.startswith("trailing")
        if mfe >= 1.0:
            cause = "GAVE BACK"
        elif stopped and reached_2r:
            cause = "SHAKEOUT"
        elif not stopped and not why.startswith("LIQ") and reached_2r:
            cause = "EARLY EXIT"
        elif further:
            cause = "WRONG WAY"
        else:
            cause = "CHOP"
        rows.append({"cause": cause, "coin": coin, "side": x["side"], "opened": x.get("opened_at"), "loss": tot,
                     "why": why[:60], "mfe_r": mfe, "mae_r": x.get("mae_r"), "held_h": round((x.get("held_min") or 0) / 60, 1),
                     "lev": x.get("leverage"), "stop_pct": round(100 * r / entry, 2) if entry else None,
                     "ctx": x.get("ctx") or {}})
    by = {}
    for r_ in rows:
        b = by.setdefault(r_["cause"], {"n": 0, "usd": 0.0})
        b["n"] += 1
        b["usd"] = round(b["usd"] + r_["loss"], 2)
    return rows, by


def compare(trades):
    """Winners vs losers: the setup's numbers, the session, the coin, the side."""
    def ny_hour(x):
        ts = x.get("opened_ts") or 0
        return int(((ts + perps.ny_offset(ts)) % 86400) // 3600)

    def sess(h):
        return "Asia (20-02 NY)" if h >= 20 or h < 2 else "London (02-08)" if h < 8 else "New York (08-16)" if h < 16 else "evening (16-20)"
    groups = {}
    for x in trades:
        tot = x.get("total") or 0.0
        ctx = x.get("ctx") or {}
        for key in (f"coin {x['coin']}", f"side {x['side']}", f"session {sess(ny_hour(x))}",
                    "trend " + ("weak <0.6" if (ctx.get("strength") or 0) < 0.6 else "mid 0.6-1.2" if (ctx.get("strength") or 0) < 1.2 else "strong 1.2+"),
                    "volatility " + ("calm <0.6%/h" if (ctx.get("atr_pct") or 0) < 0.6 else "normal 0.6-1.2%/h" if (ctx.get("atr_pct") or 0) < 1.2 else "wild 1.2%+/h")):
            g = groups.setdefault(key, {"n": 0, "won": 0, "usd": 0.0})
            g["n"] += 1
            g["won"] += tot > 0
            g["usd"] = round(g["usd"] + tot, 2)
    for g in groups.values():
        g["win_pct"] = round(100 * g["won"] / g["n"]) if g["n"] else 0
    return dict(sorted(groups.items()))


# ---------- the whole lab ----------
def run_lab(env, cache_dir, days=365, say=lambda s: None, variants=VARIANTS):
    t_start = time.time()
    base_cfg = perps.PerpConfig(env)
    h1data = load(base_cfg.coins, days, cache_dir, say)
    h1data = {c: v for c, v in h1data.items() if len(v) > 400}
    if not h1data:
        raise RuntimeError("no price history came back")
    first = max(v[0]["t"] for v in h1data.values()) + 24 * 86400      # after the 4-hour charts' warm-up (140 candles)
    last = min(v[-1]["t"] for v in h1data.values()) + H1
    mid = first + (last - first) // 2
    say(f"replaying {base_cfg.style} on {round((last - first) / 86400)} days")
    base = run(h1data, base_cfg, t0=first)
    halves = [run(h1data, base_cfg, t0=first, t1=mid), run(h1data, base_cfg, t0=mid)]
    rows, by = autopsy(base["final"], h1data)
    out = {"at": _iso(time.time()), "style": base_cfg.style, "coins": list(h1data), "from": _day(first), "to": _day(last),
           "days": round((last - first) / 86400), "baseline": stats(base), "halves": [stats(h) for h in halves],
           "causes": by, "losers": rows, "groups": compare(base["final"]), "variants": [],
           "settings": {k: v for k, v in base_cfg.settings().items() if k in (
               "stop_atr", "trail_atr", "tp1_r", "tp1_pct", "be_r", "min_trend", "rsi_long", "rsi_short", "pull_atr",
               "max_loss_pct", "max_leverage", "cooldown_minutes", "max_hold_hours")}}
    env_base = {k: v for k, v in env.items() if k.startswith("PERP_")}
    for i, (name, what, change) in enumerate(variants):
        say(f"variant {i + 1}/{len(variants)}: {name}")
        try:
            cfg = _cfg(env_base, change)
            side = change.get("__side")
            full = stats(run(h1data, cfg, side, t0=first))
            hs = [stats(run(h1data, cfg, side, t0=first, t1=mid)), stats(run(h1data, cfg, side, t0=mid))]
        except Exception as exc:
            out["variants"].append({"name": name, "error": f"{type(exc).__name__}: {exc}"[:120]})
            continue
        better = all(h["ret_pct"] is not None and b["ret_pct"] is not None and h["ret_pct"] > b["ret_pct"]
                     for h, b in zip(hs, out["halves"]))
        out["variants"].append({"name": name, "what": what, "env": {k: v for k, v in change.items() if not k.startswith("__")},
                                "full": full, "halves": hs, "better_both_halves": better})
    out["took_s"] = round(time.time() - t_start)
    return out


def report(r):
    """The result as plain text: what the bot did, why each loser lost, and which change helps in both halves."""
    if not r:
        return "The lab hasn't run yet."
    if r.get("error"):
        return f"LAB ERROR at {r.get('at')}: {r['error']}"
    b = r["baseline"]
    L = [f"DESK LAB · {r['style']} style · {', '.join(r['coins'])} · {r['from']} to {r['to']} ({r['days']} days of real 1h prices)",
         f"run at {r['at']} in {r.get('took_s')}s", "",
         "BASELINE (the current settings, $50 start, the whole balance as collateral each trade)",
         f"  {b['trades']} trades ({b['per_week']}/week), won {b['won']} ({b['win_pct']}%), $50 -> ${b['end']} ({b['ret_pct']}%), "
         f"max drawdown {b['max_dd_pct']}%, profit factor {b['profit_factor']}",
         f"  avg win ${b['avg_win']}, avg loss ${b['avg_loss']}, best ${b['best_trade']}, worst ${b['worst_trade']}, "
         f"avg hold {b['avg_hold_h']}h, liquidated {b['liquidated']}",
         f"  1st half: {r['halves'][0]['ret_pct']}% ({r['halves'][0]['trades']} trades, {r['halves'][0]['win_pct']}% won) · "
         f"2nd half: {r['halves'][1]['ret_pct']}% ({r['halves'][1]['trades']} trades, {r['halves'][1]['win_pct']}% won)",
         f"  settings: " + ", ".join(f"{k}={v}" for k, v in r["settings"].items()), "",
         "WHY THE LOSERS LOST"]
    for k, v in sorted(r["causes"].items(), key=lambda kv: kv[1]["usd"]):
        L.append(f"  {k:10s} {v['n']:3d} trades  ${v['usd']}")
    L += ["", "WINNERS VS LOSERS BY GROUP (trades, win %, $ total)"]
    for k, g in r["groups"].items():
        L.append(f"  {k:30s} {g['n']:3d}  {g['win_pct']:3d}%  ${g['usd']}")
    L += ["", "VARIANTS (one change each; return % full / 1st half / 2nd half, trades, win %, max drawdown, PF)",
          f"  {'BASELINE':22s} {b['ret_pct']:7}% / {r['halves'][0]['ret_pct']}% / {r['halves'][1]['ret_pct']}%  "
          f"{b['trades']}t {b['win_pct']}% dd{b['max_dd_pct']}% pf{b['profit_factor']}"]
    for v in sorted(r["variants"], key=lambda v: -(v.get("full", {}).get("ret_pct") or -1e9)):
        if v.get("error"):
            L.append(f"  {v['name']:22s} error: {v['error']}")
            continue
        f, h = v["full"], v["halves"]
        same = (f["trades"], f["end"], f["won"]) == (b["trades"], b["end"], b["won"])
        L.append(f"  {'*' if v['better_both_halves'] else ' '}{v['name']:21s} {f['ret_pct']:7}% / {h[0]['ret_pct']}% / {h[1]['ret_pct']}%  "
                 f"{f['trades']}t {f['win_pct']}% dd{f['max_dd_pct']}% pf{f['profit_factor']}" + ("  (same trades: no effect)" if same else ""))
    L += ["  (* = better than the baseline in BOTH halves: the only kind worth trusting)", "", "EVERY LOSING TRADE"]
    for x in sorted(r["losers"], key=lambda x: x["opened"] or ""):
        c = x.get("ctx") or {}
        L.append(f"  {str(x['opened'])[:16]} {x['coin']} {x['side']:5s} {x['lev']}x stop {x['stop_pct']}% · ${x['loss']} · {x['cause']} · "
                 f"best +{x['mfe_r']}R worst -{x['mae_r']}R · {x['held_h']}h · trend {c.get('strength')} rsi {c.get('rsi')} "
                 f"atr {c.get('atr_pct')}% · {x['why']}")
    return "\n".join(L)


class Lab:
    """Runs the lab in the background: a few minutes after the server starts (so the page is up first), then every day,
    or when asked. One run at a time; the last result is kept on the volume."""

    def __init__(self, env, data_dir):
        self.env, self.dir = dict(env), data_dir
        self.path = os.path.join(data_dir, "lab_result.json")
        self.busy, self.step, self.result = False, "", None
        try:
            with open(self.path) as f:
                self.result = json.load(f)
        except (OSError, ValueError):
            self.result = None
        self.days = int(float(env.get("LAB_DAYS", "365") or 365))

    def start(self, why="asked"):
        if self.busy:
            return 409, "the lab is already running: " + self.step
        self.busy, self.step = True, "starting"

        def job():
            try:
                res = run_lab(self.env, self.dir, self.days, say=lambda s: setattr(self, "step", s))
            except Exception as exc:          # the lab never touches the trading
                log.exception("lab failed")
                res = {"error": f"{type(exc).__name__}: {exc}"[:300], "at": _iso(time.time())}
            self.result = res
            try:
                with open(self.path + ".tmp", "w") as f:
                    json.dump(res, f)
                os.replace(self.path + ".tmp", self.path)
            except OSError:
                pass
            self.busy, self.step = False, ""
        threading.Thread(target=job, daemon=True, name="lab").start()
        return 200, "the lab started: it takes a few minutes"

    def auto(self):
        """Every hour: start a run when the last one is a day old (or missing)."""
        def loop():
            time.sleep(180)
            while True:
                at = (self.result or {}).get("at")
                try:
                    age = time.time() - datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                except (TypeError, ValueError):
                    age = math.inf
                if age > 86400 and not self.busy:
                    self.start("daily")
                time.sleep(3600)
        threading.Thread(target=loop, daemon=True, name="lab-auto").start()

    def status(self):
        return {"busy": self.busy, "step": self.step, "result": self.result, "text": report(self.result)}
