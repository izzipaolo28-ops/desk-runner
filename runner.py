# runner.py
"""Everything in one process, for Railway (start command: python runner.py):

  1. the desk: judge.py (FastAPI + TypeSafe SDK) on 127.0.0.1:8081, reachable only from inside this process
  2. the loop: main.run_once every CYCLE_SECONDS; while a position is open, RISK every RISK_POLL_SECONDS instead
     (a held desk does not scan)
  3. the dashboard on $PORT: GET / (the page), GET /api/status (JSON), POST /api/pause and /api/resume
     (header X-Admin-Token = ADMIN_TOKEN; without ADMIN_TOKEN set, nobody can pause from the web)

Variables: see README.md. Starts in paper mode unless LIVE_MODE=true.
"""
import hmac
import json
import logging
import math
import os
import secrets
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# where the desk keeps what must survive a redeploy: DATA_DIR if set, else the Railway volume, else ./data
DATA_DIR = (os.environ.get("DATA_DIR", "").strip() or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
            or str(HERE / "data"))
os.environ["DATA_DIR"] = DATA_DIR


def _storage():
    """Is the state on a Railway volume? Without one, every deploy starts the paper bank, the trades and the
    benches over (the page says so). Railway sets RAILWAY_VOLUME_MOUNT_PATH only when a volume is attached."""
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    railway = any(os.environ.get(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID",
                                              "RAILWAY_SERVICE_ID"))
    on = False
    if mount:
        try:
            on = os.path.commonpath([os.path.realpath(DATA_DIR), os.path.realpath(mount)]) == os.path.realpath(mount)
        except ValueError:
            on = False
    return {"railway": railway, "volume": on, "mount": mount or None}


STORAGE = _storage()
MIN_ADMIN_TOKEN = 16
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("DESK_DB", os.path.join(DATA_DIR, "desk.db"))
os.environ.setdefault("JUDGE_LOG", os.path.join(DATA_DIR, "judge_log.jsonl"))
os.environ.setdefault("DESK_SECRET", secrets.token_urlsafe(32))       # internal: bot and desk share this process
DESK_PORT = int(os.environ.get("DESK_PORT", "8081"))
os.environ.setdefault("JUDGE_URL", f"http://127.0.0.1:{DESK_PORT}")

import book  # noqa: E402
import collect  # noqa: E402
import filter as desk_filter  # noqa: E402  (the desk's filter.py, not the builtin)
import main  # noqa: E402
import pick as desk_pick  # noqa: E402
import snipe  # noqa: E402
import thresholds  # noqa: E402
from jupiter import Jupiter  # noqa: E402
from public_list import PublicList  # noqa: E402
from seats import Config, Seats, State, _now  # noqa: E402
from seats import record as seats_record  # noqa: E402
import solana_wallet  # noqa: E402
from solana_wallet import RPC, Keypair, WalletKeyError, b58decode, b58encode  # noqa: E402

log = logging.getLogger("runner")
NETS = tuple(n.strip() for n in os.environ.get("NETS", "solana").split(",") if n.strip())
CYCLE_SECONDS = float(os.environ.get("CYCLE_SECONDS", 300))     # 5 minutes: three times main's 15, at Jev's ~$0.0001 a scan
DESK_TITLE = os.environ.get("DESK_TITLE", "GROKBOT + JEV").strip()[:40] or "GROKBOT + JEV"
DESK_TAGLINE = os.environ.get("DESK_TAGLINE", "JEV PICKS, CODE TRADES").strip()[:60]


def _fomo_link(raw):
    """FOMO_LINK: your FOMO app's link for one coin, with {mint} where the coin's address goes. Unset: no link."""
    raw = (raw or "").strip()
    ok = raw.startswith("https://") and "{mint}" in raw and len(raw) <= 300 and not any(c in raw for c in "\"'<> \\")
    return raw if ok else None


FOMO_LINK = _fomo_link(os.environ.get("FOMO_LINK"))


# THE DESK's coins: older coins with real volume that can still 2x-3x, read from GeckoTerminal's trending lists over
# the last hour, 6 hours and day. Minutes-old launches are the sniper's (they'd only be cut for age here).
_tw = os.environ.get("DESK_TRENDING", "").strip() or os.environ.get("TRENDING_WINDOW", "").strip() or "1h,6h,24h"
DESK_TRENDING = [w for w in dict.fromkeys(x.strip() for x in _tw.split(",")) if w in ("5m", "1h", "6h", "24h")] or ["1h"]
TRENDING_WINDOW = DESK_TRENDING[0]
try:
    TRENDING_PAGES = max(0, min(3, int(os.environ.get("TRENDING_PAGES", "1"))))
except ValueError:
    TRENDING_PAGES = 1
try:
    DESK_NEW_POOL_PAGES = max(0, min(3, int(os.environ.get("DESK_NEW_POOL_PAGES", "0"))))
except ValueError:
    DESK_NEW_POOL_PAGES = 0
try:
    DESK_METAS = max(0, min(200, int(os.environ.get("DESK_METAS", "60"))))   # coins from DexScreener's trending narratives
except ValueError:
    DESK_METAS = 60
try:
    TEST_SWAP_USD = min(20.0, max(0.5, float(os.environ.get("TEST_SWAP_USD", "1"))))
except ValueError:
    TEST_SWAP_USD = 1.0
DESK_GATE = "off" if os.environ.get("DESK_GATE", "on").strip().lower() == "off" else "on"
try:
    DESK_GATE_SCORE = max(0.0, min(100.0, float(os.environ.get("DESK_GATE_SCORE", "65"))))
except ValueError:
    DESK_GATE_SCORE = 65.0
# The night of Oct 13-14: the desk's picks were most of the losses (Catnip, Altai, Buddy, DOPE, ARCADES, ROSE): scores
# of 42-61 against a bar of 25, buyers only 1.0-1.2x the sellers, micro caps on $26-28K pools, bought right after a
# +17-28% candle. The desk now holds its picks to the sniper's standard.
try:
    DESK_MIN_MCAP_USD = max(0.0, float(os.environ.get("DESK_MIN_MCAP_USD", "150000")))
    DESK_MAX_M5_PCT = max(0.0, float(os.environ.get("DESK_MAX_M5_PCT", "15")))
    DESK_MIN_BUYERS_X = max(0.0, float(os.environ.get("DESK_MIN_BUYERS_X", "1.5")))
    DESK_LOSS_STREAK = max(0, int(float(os.environ.get("DESK_LOSS_STREAK", "2"))))
    DESK_COOLDOWN_MINUTES = max(0.0, float(os.environ.get("DESK_COOLDOWN_MINUTES", "60")))
except ValueError:
    DESK_MIN_MCAP_USD, DESK_MAX_M5_PCT, DESK_MIN_BUYERS_X, DESK_LOSS_STREAK, DESK_COOLDOWN_MINUTES = 150000.0, 15.0, 1.5, 2, 60.0
# THE DESK waits for a retrace: a pick isn't bought at once (PQC: picked at $258K while it slid from $300K). It waits up to
# DESK_RETRACE_MINUTES for the price to pull back DESK_RETRACE_PCT under its high since the pick and bounce (green 5 minutes,
# buyers ahead), with the coin still up over 6 hours; down DESK_RETRACE_MAX_DROP since the pick, the pick is dropped.
# PQC: bought with nobody buying it. A desk pick with fewer than this many buys in the last 5 minutes isn't bought.
try:
    DESK_MIN_BUYS_5M = max(0, int(float(os.environ.get("DESK_MIN_BUYS_5M", "5"))))
except ValueError:
    DESK_MIN_BUYS_5M = 5
try:
    DESK_RETRACE_PCT = max(0.0, min(50.0, float(os.environ.get("DESK_RETRACE_PCT", "8"))))
    DESK_RETRACE_MINUTES = max(1.0, min(240.0, float(os.environ.get("DESK_RETRACE_MINUTES", "45"))))
    DESK_RETRACE_MAX_DROP = max(1.0, min(90.0, float(os.environ.get("DESK_RETRACE_MAX_DROP", "20"))))
    # no pullback but the buyers take over hard: up DESK_BREAKOUT_PCT since the pick, at its high, green 5 minutes (not a
    # 30%+ candle), buyers 1.5x the sellers, 5-minute volume 1.5x the hour's pace, still up this hour, seen on 2 checks in a row
    DESK_BREAKOUT_PCT = max(0.0, min(100.0, float(os.environ.get("DESK_BREAKOUT_PCT", "10"))))
except ValueError:
    DESK_RETRACE_PCT, DESK_RETRACE_MINUTES, DESK_RETRACE_MAX_DROP, DESK_BREAKOUT_PCT = 8.0, 45.0, 20.0, 10.0
# THE DIAL: how hard to push today, 0.4 to 1, from the bots' own last 24 hours, today's losses and how the hot coins are
# doing this hour. Every automatic ticket is multiplied by it; under DIAL_EARLY_MIN there are no early bets that day.
DIAL = "off" if os.environ.get("DIAL", "on").strip().lower() == "off" else "on"
try:
    DIAL_MIN = max(0.1, min(1.0, float(os.environ.get("DIAL_MIN", "0.4"))))
    DIAL_EARLY_MIN = max(0.0, min(1.0, float(os.environ.get("DIAL_EARLY_MIN", "0.75"))))
except ValueError:
    DIAL_MIN, DIAL_EARLY_MIN = 0.4, 0.75
SCAN_NOW_COOLDOWN = 60          # seconds: "Scan now" can't fire twice inside a minute (GeckoTerminal, Jev credits)
GATE_RETRY_SECONDS = 60         # the desk's pick failed the safety checks: the next scan starts this soon


def _f(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def universe(pages=None):
    """The desk's coins: GeckoTerminal's trending pools over each window in DESK_TRENDING (1h, 6h, 24h: what has been
    trading for a while, not what launched a minute ago), then new pools only if DESK_NEW_POOL_PAGES asks for them.
    One GeckoTerminal slot per page. A list failing is recorded in collect.UNIVERSE_FAILURES; only all of them failing
    stands the cycle down."""
    if DESK_NEW_POOL_PAGES:
        fresh = collect.universe(nets=NETS, pages=DESK_NEW_POOL_PAGES)    # clears the failure list
    else:
        collect.UNIVERSE_FAILURES.clear()
        fresh = []
    hot = []
    for w in DESK_TRENDING if TRENDING_PAGES else ():
        hot += collect.trending(nets=NETS, pages=TRENDING_PAGES, duration=w)
    out, seen = [], set()
    for tid in hot + fresh:
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def why(stats):
    """'free: age 31, volume 9 · trade: no_sells 2'. The counts alone can't tell a bug from a quiet market."""
    parts = []
    failed = stats.get("universe_failed") or []
    if failed:                                    # a short list is GeckoTerminal refusing, not a quiet market
        parts.append(f"list failed ({len(failed)}): " + "; ".join(f[:90] for f in failed[:2]))
    for stage in ("free", "trade", "chain", "soft"):
        reasons = stats.get(stage) or {}
        if reasons:
            top = sorted(reasons.items(), key=lambda kv: -kv[1])
            parts.append(f"{stage}: " + ", ".join(f"{k} {v}" for k, v in top[:4]))
    return " · ".join(parts)


class SlotBook:
    """book.py, as main.run_once sees it: "held" only when every slot is full, so the desk keeps scanning while it
    holds fewer than MAX_POSITIONS coins. Bench and everything else is the real book."""

    def __init__(self, runner):
        self.runner = runner

    def held(self):
        return None if self.runner.desk_slots_free() else book.held()

    def __getattr__(self, name):
        return getattr(book, name)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts is not None else None


def _coin(t):
    return {"ticker": t.get("ticker"), "mint": t.get("addr") or t.get("address")}


class Runner:
    def __init__(self, cfg, seats, state, judge, lister, clock=time.time):
        self.cfg, self.seats, self.state, self._judge, self.lister, self.clock = cfg, seats, state, judge, lister, clock
        self.last_cycle = self.last_risk = self.last_watch = None
        self.last_bank = None
        self.test_requested = False
        self.coin, self.seen_n, self.seen_of, self.coin_no, self.judge_ms = {}, 0, None, None, 0
        main.universe = self.universe             # trending first, then the coins of DexScreener's trending narratives
        main.book = SlotBook(self)                # main stands down only when every slot is full
        self._instrument()
        self.sniper = snipe.Sniper(self)          # your own checks, the wallets you follow, the fast lane
        self._dial = None
        self.seats.dial_fn = lambda: self.dial()["value"]        # every automatic ticket, times today's dial
        self.bot_sol, self.bot_sol_at, self.bot_sol_err = None, None, None
        self.money_request, self.last_withdraw = None, None
        self._restore_money()
        self._watching = False
        self.sell_requests = []                   # coins you asked to sell from the page
        self.pending_desk, self.last_entry_check = {}, None   # desk picks waiting for a retrace
        self._gate_rep = None                                  # the sniper's report on the last desk pick
        collect.WAIT_HOOK = self.watch_if_due     # a scan waiting on GeckoTerminal keeps watching prices meanwhile

    # ---------- the live feed: main.run_once is the desk's code, unchanged; each step it calls is wrapped ----------
    def _instrument(self):
        st, R = self.state, self
        R.ms = {}

        def timed(fn, *a, **k):
            t0 = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                R.ms[fn.__name__] = int((time.perf_counter() - t0) * 1000)

        def feed(agent, kind, text, t, check, ms=None):
            st.feed(agent, kind, text, check=check, no=R.coin_no, ms=ms, **_coin(t))

        def shortlist(fomo, ids):
            listed = collect.shortlist(fomo, ids)
            R.seen_n, R.seen_of = 0, len(listed)
            st.feed("crawler", "info", f"scan started: {len(ids)} pools listed, {len(listed)} with market data")
            return listed

        def step(agent, name, t):
            R.coin = _coin(t)
            st.set_working(agent, name, n=R.seen_n, of=R.seen_of, **R.coin)
            R.watch_if_due()                      # a scan takes minutes: the coins held are still watched

        def free_kill(t):
            R.seen_n += 1
            R.coin_no = st.bump("coins")
            R.sniper.remember(t.get("addr"), t.get("ticker"))     # the desk's coins count for the sniper's themes too
            step("crawler", "free_kill", t)
            if t.get("addr") in {p.get("mint") for p in st.positions()}:
                st.feed("crawler", "info", "already held: RISK is managing it", **_coin(t))
                return "already_held"
            if t.get("addr") in R.pending_desk:
                st.feed("crawler", "info", "already picked: waiting for its retrace", **_coin(t))
                return "already_held"
            k = timed(desk_filter.free_kill, t)
            st.lens(t, "free_kill", k, R.coin_no)
            feed("crawler", "cut" if k else "pass", f"cut: {k}" if k else "passed the free checks", t, "free_kill",
                 R.ms.get("free_kill"))
            if k:
                st.bump("cut")
            return k

        def trade_counts(t):
            step("crawler", "trade data", t)
            return timed(collect.trade_counts, t)

        def trade_kill(t):
            k = desk_filter.trade_kill(t)
            st.lens(t, "trade_kill", k, R.coin_no)
            ms = R.ms.get("trade_counts")
            if k:
                st.bump("cut")
                feed("crawler", "cut", f"cut: {k}", t, "trade_kill", ms)
            else:
                feed("crawler", "pass", f"trades ok: {t.get('trades_h24')} in 24h", t, "trade_kill", ms)
            return k

        def dossier(t, use_gt=True):
            step("vet", "dossier", t)
            try:
                d = timed(collect.dossier, t, use_gt=use_gt)
            except collect.DossierFailed as exc:
                feed("vet", "error", f"dossier failed: {str(exc)[:80]}", t, "dossier", R.ms.get("dossier"))
                raise
            if d.get("gt_info_status") not in (None, "ok"):
                feed("vet", "info", f"GeckoTerminal {d['gt_info_status'][:70]}", t, "dossier", R.ms.get("dossier"))
            return d

        def chain_kill(d):
            step("vet", "chain_kill", d)
            k = desk_filter.chain_kill(d)
            st.lens(d, "chain_kill", k, R.coin_no)
            ms = R.ms.get("dossier")
            if k:
                st.bump("cut")
                st.bump("rugs")
                feed("vet", "cut", f"cut: {k}", d, "chain_kill", ms)
            else:
                tw = d.get("top_wallet_percent")
                feed("vet", "pass", "chain ok" + (f": top wallet {tw:.1f}%" if isinstance(tw, (int, float)) else ""),
                     d, "chain_kill", ms)
                handle = d.get("x_handle")       # SOCIAL's honest state for this coin: what exists, what was read
                st.agent("social", (f"@{handle} is the token's published X account. Posts not read: no X source "
                                    "is set, so the ticket takes the x0.60 cut" if handle else
                                    "this token publishes no X account: ticket x0.60, never a look-alike handle"),
                         None, "x account")
            R.judge_ms = 0
            return k

        def soft_kill(ans, d=None, asked=()):
            k = desk_filter.soft_kill(ans, d, asked)
            st.lens(d or {}, "soft_kill", k, R.coin_no, answers=ans)
            if k:
                st.bump("cut")
            feed("chief" if not k else "vet", "cut" if k else "pass",
                 f"Jev says no: {k}" if k else "survived every check: goes to the pick", d or {}, "soft_kill",
                 getattr(R, "judge_ms", None))
            return k

        def pick(ask, survivors):
            if survivors:
                st.set_working("chief", "pick", n=len(survivors), of=len(survivors))
            order, why = desk_pick.pick(ask, survivors)
            if order:
                st.bump("picks")
                tok = order["token"]
                st.feed("chief", "pick", f"PICK, confidence {_fmt(order.get('confidence'))}",
                        tok.get("ticker"), tok.get("address"))
            elif survivors:
                st.feed("chief", "cut", f"no pick: {why}")
            return order, why

        main.shortlist, main.free_kill, main.trade_counts, main.trade_kill = shortlist, free_kill, trade_counts, trade_kill
        main.dossier, main.chain_kill, main.soft_kill, main.pick = dossier, chain_kill, soft_kill, pick

    # the judge, with every answer kept for the dashboard's agent cards
    def judge(self, set_name, state):
        seat = {"market": "scan", "social": "social", "pick": "chief"}.get(set_name, "vet")
        if set_name != "pick":
            self.state.set_working(seat, f"asking Jev: {set_name}", n=self.seen_n, of=self.seen_of, **self.coin)
        t0 = time.perf_counter()
        r = self._judge(set_name, state)
        self.judge_ms = getattr(self, "judge_ms", 0) + int((time.perf_counter() - t0) * 1000)
        self.state.bump("jev")
        if set_name != "pick":
            self.state.feed(seat, "ask", f"Jev answered the {set_name} questions", **self.coin)
        answers = r.get("answers") or {}
        row = {"time": _now(), "set": set_name, "model": r.get("model"),
               "answers": {k: {f: v.get(f) for f in ("choice", "noul", "score", "confidence")} for k, v in answers.items()
                           if isinstance(v, dict)}}
        with self.state.lock:
            self.state.data["judge"] = ([row] + self.state.data["judge"])[:30]
        a = row["answers"]
        if set_name == "market" and "shape" in a:
            crowd = ((answers.get("shape") or {}).get("probabilities") or {}).get("crowd")
            self.state.agent("scan", f"shape {a['shape'].get('choice')}, momentum spent "
                                     f"{_fmt(a.get('momentum_already_spent', {}).get('noul'))}", crowd, "crowd")
        elif set_name in ("solana", "bsc", "robinhood"):
            conc = a.get("concentration_is_exit_risk", {}).get("noul")
            extra = next((f"{k.replace('_', ' ')} {a[k].get('choice')}" for k in ("authority_risk", "sell_side_risk", "data_coverage")
                          if k in a), "")
            self.state.agent("vet", f"{set_name}: {extra}".strip(": "), conc, "concentration")
        elif set_name == "social":
            self.state.agent("social", f"official account {_fmt(a.get('account_is_the_project', {}).get('noul'))}, "
                                       f"recycled {_fmt(a.get('recycled_account', {}).get('noul'))}",
                             a.get("account_is_the_project", {}).get("noul"), "official")
        elif set_name == "pick":
            self.state.agent("chief", f"worth trading {_fmt(a.get('worth_trading_at_all', {}).get('noul'))}"
                                      + (f", best {a['best'].get('choice')}" if "best" in a else ""),
                             a.get("worth_trading_at_all", {}).get("noul"), "worth")
        return r

    def universe(self, pages=None):
        """The desk's list: GeckoTerminal's trending windows, then the coins inside the narratives trending on DexScreener
        right now (read at most every 10 minutes, shared with the sniper). A narrative coin under the desk's minimum age
        is cut for free like any other."""
        ids = universe(pages)
        if DESK_METAS and "solana" in NETS:
            try:
                self.sniper.metas.refresh()
            except Exception as exc:                  # a narrative list that can't be read never stops the scan
                log.warning("metas for the desk: %s", exc)
            seen = set(ids)
            for mint in list(self.sniper.metas.by_mint)[:DESK_METAS]:
                tid = f"{mint}:{collect.FOMO_NET['solana']}"
                if tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
        return ids

    def watch_if_due(self):
        """WATCH between the steps of a scan or a check (they can take minutes, and a memecoin can drop 40% in one).
        Never touches the scan's own 'working' marker; never runs inside itself."""
        if self._watching or not self.cfg.watch or not self.state.positions():
            return
        now = self.clock()
        if self.last_watch is not None and now - self.last_watch < self.cfg.watch_seconds:
            return
        self._watching = True
        try:
            self.last_watch = now
            self.seats.watch_poll(clock=self.clock)
        except Exception as exc:
            self.state.error(f"watch during a scan: {type(exc).__name__}: {exc}")
        finally:
            self._watching = False

    def gate(self, order):
        """Every desk pick gets the sniper's full check before a cent goes in: RugCheck, locked liquidity, the dev
        wallet, bundles, copycats, people able to sell, buyers vs sellers right now. (True, why) to buy."""
        tok = order["token"]
        self._gate_rep = None
        if DESK_GATE == "off":
            return True, "gate off"
        # Normal coins: the safety checks only (rugs, copycats, can't sell, already ran and cooling, sellers ahead).
        # Not the sniper's early-coin rules (its age and size window, narrative, hype, 2x room): those are the sniper's.
        rep = self.sniper.check(tok["address"], "desk pick", purpose="desk")
        ratio = (rep.get("flow") or {}).get("ratio15")
        m5 = ((rep.get("facts") or {}).get("chg") or {}).get("m5")
        h1 = ((rep.get("facts") or {}).get("chg") or {}).get("h1")
        blind = next((w for w in rep.get("wait") or [] if w.startswith("the bubble map couldn't be read")), None)
        ran = next((w for w in rep.get("wait") or [] if w.startswith("it just ran")), None)
        learned = next((w for w in rep.get("wait") or [] if w.startswith("learned from its own trades")), None)
        b5 = (((rep.get("facts") or {}).get("buys") or {}).get("m5"))
        s5 = (((rep.get("facts") or {}).get("sells") or {}).get("m5"))
        mc = _f((rep.get("facts") or {}).get("mcap"))
        if rep["verdict"] == "SKIP":
            ok, why = False, "; ".join(rep.get("veto") or [])[:160] or "a rug check failed"
        elif blind:
            ok, why = False, blind[:160]
        elif ran:
            ok, why = False, ran[:160]
        elif learned:
            ok, why = False, learned[:160]
        elif isinstance(b5, int) and b5 < DESK_MIN_BUYS_5M:
            ok, why = False, (f"nobody is buying it right now ({b5} buys vs {s5 if s5 is not None else '?'} sells in 5 min): "
                              "a coin nobody buys only falls")
        elif mc is not None and DESK_MIN_MCAP_USD > 0 and mc < DESK_MIN_MCAP_USD:
            ok, why = False, (f"a micro cap (MC {snipe.money(mc)}, under {snipe.money(DESK_MIN_MCAP_USD)}): those are the sniper's "
                              "early bets, with their stricter checks and half the ticket")
        elif (isinstance(b5, int) and isinstance(s5, int) and s5 > 0 and DESK_MIN_BUYERS_X > 0 and b5 < s5 * DESK_MIN_BUYERS_X):
            ok, why = False, f"buyers aren't clearly ahead ({b5} buys vs {s5} sells in 5 min, it wants {DESK_MIN_BUYERS_X:g}x)"
        elif isinstance(m5, (int, float)) and DESK_MAX_M5_PCT > 0 and m5 >= DESK_MAX_M5_PCT:
            ok, why = False, f"it's spiking right now ({m5:+.1f}% in 5 min): not buying the top of the candle"
        elif isinstance(h1, (int, float)) and h1 <= -25:
            ok, why = False, f"it's a falling knife: {h1:+.0f}% this hour (a bounce in a falling coin isn't an entry)"
        elif isinstance(m5, (int, float)) and self.cfg.snipe_max_m5_pct > 0 and m5 >= self.cfg.snipe_max_m5_pct:
            ok, why = False, f"it's spiking right now ({m5:+.1f}% in 5 min): not buying the top of the candle"
        elif (isinstance(m5, (int, float)) and m5 <= -8) or (isinstance(ratio, (int, float)) and ratio < 0.7):
            ok, why = False, "it's dumping right now" + (f" ({m5:+.1f}% in 5 min)" if isinstance(m5, (int, float)) and m5 <= -8 else
                                                          f" (sellers {1 / ratio:.1f}x buyers over 15 min)" if ratio else "")
        elif (rep.get("score") or 0) < DESK_GATE_SCORE:
            ok, why = False, f"sniper score {rep.get('score')} is under {DESK_GATE_SCORE:g}"
        else:
            ok, why = True, f"sniper score {rep.get('score')}"
        self._gate_rep = rep
        rep["action"] = ("desk pick: passed the sniper's checks" + (", waiting for a retrace" if DESK_RETRACE_PCT > 0 else ", buying")
                         if ok else f"desk pick not bought: {why}")
        self.sniper._save(rep)
        if not ok:
            self.state.feed("chief", "cut", f"desk pick stopped by the sniper's checks: {why}", tok.get("ticker"), tok["address"])
        return ok, why

    # ---------- MONEY: FAKE (paper) or REAL (the bot wallet's SOL), your wallet, funding, withdrawals ----------
    def money_mode(self):
        return "real" if self.cfg.live else "fake"

    def owner_wallet(self):
        env = os.environ.get("OWNER_WALLET", "").strip()
        if env:
            return env, True                                  # set in Railway: the page can't change it
        return self.state.data.get("owner_wallet"), False

    def _restore_money(self):
        """The mode you picked on the page survives a redeploy. REAL comes back only if it still can run."""
        want = self.state.data.get("money_mode")
        if want == "fake":
            self.cfg.live = False
        elif want == "real":
            problems = self.real_problems(check_balance=False)
            if problems:
                self.cfg.live = False
                self.state.error("REAL money was on, but " + problems[0] + ": back on FAKE money until that's fixed")
            else:
                self.cfg.live = True
        self._sync_curve()
        self.seats.mirror_daily()                    # the day fields show the money that's on

    def real_problems(self, check_balance=True):
        """Everything standing between this desk and real money, in plain words. [] = ready."""
        out = []
        if self.seats.kp is None:
            out.append("there's no bot wallet: make a NEW wallet just for the bot and put its private key in Railway's "
                       "variables, under the name shown below (never on this page, never in a chat)")
        if STORAGE["railway"] and not STORAGE["volume"]:
            out.append("this service has no Railway volume: a redeploy would forget the real coins it holds")
        if PRACTICE["active"]:
            out.append("PRACTICE_MODE is on: real money runs on the real rules, turn it off in Railway first")
        if check_balance and self.seats.kp is not None:
            sol = self.read_bot_sol(force=True)
            need = self.cfg.fee_reserve_sol + 0.01
            if sol is None:
                out.append(f"the bot wallet's balance can't be read right now ({self.bot_sol_err or 'no answer'})")
            elif sol < need:
                out.append(f"the bot wallet holds {sol:.4f} SOL: fund it first (it keeps {self.cfg.fee_reserve_sol:g} SOL for fees)")
        return out

    def read_bot_sol(self, force=False):
        """The bot wallet's SOL, read at most once a minute (the page polls; the chain isn't asked every time).
        force: read it now, and None if that read fails (never an old figure standing in for a fresh one)."""
        if self.seats.kp is None:
            return None
        now = time.time()
        if force or self.bot_sol_at is None or now - self.bot_sol_at >= 60:
            self.bot_sol_at = now
            try:
                self.bot_sol, self.bot_sol_err = self.seats.rpc.sol_lamports(self.seats.kp.address) / solana_wallet.LAMPORTS_PER_SOL, None
            except Exception as exc:
                self.bot_sol_err = str(exc)[:120]
                if force:
                    return None
        return self.bot_sol

    def set_money(self, mode, confirm=None):
        """From the page: checks, then queues the switch. The loop makes it between two steps, never in the middle of a
        scan, a buy or a sale (a buy sized on one money is never filled with the other)."""
        mode = str(mode or "").strip().lower()
        if mode not in ("fake", "real"):
            return 400, "pick fake or real"
        if mode == self.money_mode():
            self.money_request = None
            return 200, f"already on {mode.upper()} money"
        if mode == "real":
            problems = self.real_problems()
            if problems:
                return 409, "not yet: " + problems[0]
            if str(confirm or "").strip() != "REAL":
                return 400, "type REAL to confirm"
        self.money_request = mode
        return 202, f"switching to {mode.upper()} money at the next safe moment: within seconds, or once the scan running now ends"

    def apply_money(self):
        """The loop's side of the switch: nothing else is running, so nothing is half done in the old money."""
        mode, self.money_request = self.money_request, None
        if mode not in ("fake", "real") or mode == self.money_mode():
            return None
        if mode == "real":
            problems = self.real_problems()
            if problems:
                self.state.feed("chief", "error", ("REAL money not switched on: " + problems[0])[:160])
                return "money: not switched"
        for m in list(self.pending_desk):                     # a pick made with the other money isn't bought with this one
            try:
                self._drop_entry(m, self.clock(), f"the money switched to {mode.upper()}")
            except KeyError:
                pass
        self.sniper.pending.clear()
        if self.sniper.forced or any(q.get("buy") for q in self.sniper.queue):
            self.sniper.forced.clear()                        # a "buy it" tapped on the other money isn't bought with this one
            for q in self.sniper.queue:
                q["buy"] = False
            self.state.feed("sniper", "info", "the money switched: buys you asked for before the switch were cancelled, ask again")
        self.test_requested = False
        self.cfg.live = mode == "real"
        self.last_bank = None
        with self.state.lock:
            self.state.data["money_mode"] = mode
            self.state.save()
        self._sync_curve()
        self.seats.mirror_daily()
        held = [p for p in self.state.positions() if p.get("mode") == ("paper" if mode == "real" else "live")]
        if mode == "real":
            line = (f"REAL MONEY ON: the bots buy with the bot wallet's SOL ({self.bot_sol or 0:.4f} SOL), at most "
                    f"${self.cfg.real_max_ticket_usd:g} a buy, and stop for the day at -{self.cfg.max_daily_loss_pct:g}%"
                    + (f". The {len(held)} paper coin(s) still held are finished on paper" if held else ""))
        else:
            line = ("FAKE MONEY: new buys are paper" + (f". The {len(held)} real coin(s) still held are managed and sold for real"
                                                       if held else ""))
        self.state.feed("chief", "info", line[:160])
        self.state.agent("chief", line, None, "money")
        self.snapshot()
        return "money: switched"

    def _sync_curve(self):
        """Each money keeps its own balance chart: after a switch, the chart on show is the new money's."""
        with self.state.lock:
            d, mode = self.state.data, self.seats.money()
            shown = d.get("equity_mode")
            if shown is not None and shown != mode:
                d.setdefault("equity_saved", {})[shown] = d.get("equity") or []
                d["equity"] = d["equity_saved"].get(mode) or []
            d["equity_mode"] = mode                               # a state from before: its chart is this money's
            self.state.save()

    def owner_message(self, address, hour=None):
        """What your wallet signs to become the withdrawal wallet: names both wallets and the hour, so an old
        signature can't be reused later or on another bot."""
        hour = hour or datetime.now(timezone.utc).strftime("%Y-%m-%d %H")
        return (f"desk-runner: send this bot's withdrawals to {address} (bot wallet "
                f"{self.seats.kp.address if self.seats.kp else 'none'}, {hour} UTC)")

    def register_owner(self, address, signature, message):
        with self.seats.money_lock:                  # two registrations at once: the first wins, the second is told
            return self._register_owner(address, signature, message)

    def _register_owner(self, address, signature, message):
        address = str(address or "").strip()
        current, locked = self.owner_wallet()
        if locked:
            return 409, "your wallet is set in Railway (OWNER_WALLET): change it there"
        if current:
            return 409, (f"withdrawals already go to {current[:4]}…{current[-4:]}: to change it, put the new address in "
                         "Railway's variables as OWNER_WALLET")
        if self.seats.kp is None:
            return 409, "there's no bot wallet yet (BOT_WALLET_KEY in Railway)"
        try:
            if len(b58decode(address)) != 32:
                raise ValueError
        except ValueError:
            return 400, "that isn't a Solana address"
        now = datetime.now(timezone.utc)
        ok_msgs = {self.owner_message(address, (now - timedelta(hours=h)).strftime("%Y-%m-%d %H")) for h in (0, 1)}
        if message not in ok_msgs:
            return 400, "the signed text doesn't match: reload the page and connect again"
        sig = _sig_bytes(signature)
        if sig is None or not solana_wallet.verify_signed_message(address, message.encode(), sig):
            return 400, "the signature doesn't check out: connect again and approve the message in your wallet"
        with self.state.lock:
            self.state.data["owner_wallet"] = address
            self.state.save()
        self.state.feed("chief", "info", f"your wallet {address[:4]}…{address[-4:]} is now the only place withdrawals can go")
        return 200, f"withdrawals go to {address[:4]}…{address[-4:]}"

    def fund_message(self, from_addr, sol):
        """The transfer your wallet signs to fund the bot (it shows in your wallet's own approval screen)."""
        if self.seats.kp is None:
            return 409, "there's no bot wallet yet (BOT_WALLET_KEY in Railway)"
        amount = _sol_amount(sol)
        if amount is None:
            return 400, "send an amount of SOL"
        lamports = int(round(amount * solana_wallet.LAMPORTS_PER_SOL))
        if not MIN_TRANSFER_LAMPORTS <= lamports <= 1000 * solana_wallet.LAMPORTS_PER_SOL:
            return 400, "the amount must be between 0.001 and 1000 SOL"
        try:
            msg = solana_wallet.transfer_message(str(from_addr or "").strip(), self.seats.kp.address, lamports,
                                                 self.seats.rpc.latest_blockhash())
        except ValueError as exc:
            return 400, str(exc)
        except Exception as exc:
            return 503, f"the Solana network didn't answer: {str(exc)[:100]}"
        return 200, {"message": b58encode(msg), "lamports": lamports, "to": self.seats.kp.address}

    def withdraw(self, sol):
        """Real SOL out of the bot wallet, only ever to your wallet. Keeps enough for fees while real coins are held or
        REAL is on. Holds the money lock from the balance read to the confirmation: no real buy reads the wallet halfway."""
        with self.seats.money_lock:
            return self._withdraw(sol)

    def _withdraw(self, sol):
        owner, _ = self.owner_wallet()
        if self.seats.kp is None:
            return 409, "there's no bot wallet (BOT_WALLET_KEY in Railway)"
        if not owner:
            return 409, "connect your wallet first: withdrawals only go to the wallet you connected"
        everything = str(sol).strip().lower() == "all"
        amount = None if everything else _sol_amount(sol)
        if not everything and amount is None:
            return 400, "send an amount of SOL, or all"
        busy = [p for p in self.state.positions() if p.get("mode") == "live" and p.get("status") != "open"]
        if busy:
            return 409, "a real buy is still landing: try again in a minute"
        last = self.last_withdraw
        if last and time.time() - last["at"] < 120:            # the last one may still be landing: never send twice
            try:
                st = self.seats.rpc.signature_status(last["sig"])
            except Exception:
                st = None
            if not st or st == "processed":
                return 409, f"the last withdrawal (tx {last['sig'][:8]}…) is still landing: check again in a minute"
        try:
            balance = self.seats.rpc.sol_lamports(self.seats.kp.address)
        except Exception as exc:
            return 503, f"the bot wallet's balance can't be read: {str(exc)[:100]}"
        fee = 5000
        holding = any(p.get("mode") == "live" for p in self.state.positions())
        reserve = int(self.cfg.fee_reserve_sol * solana_wallet.LAMPORTS_PER_SOL) if holding or self.cfg.live else 0
        most = balance - fee - reserve
        lamports = most if everything else min(most, int(round(amount * solana_wallet.LAMPORTS_PER_SOL)))
        if lamports <= 0:
            return 409, (f"nothing to withdraw: the bot wallet holds {balance / solana_wallet.LAMPORTS_PER_SOL:.4f} SOL"
                         + (f" and keeps {self.cfg.fee_reserve_sol:g} for fees while real coins are held or REAL is on" if reserve else ""))
        if lamports < MIN_TRANSFER_LAMPORTS:
            return 400, "the smallest withdrawal is 0.001 SOL"
        left = balance - fee - lamports
        if 0 < left < MIN_TRANSFER_LAMPORTS:                  # under rent: Solana refuses an account left with dust
            return 400, "leave at least 0.001 SOL in the bot wallet, or withdraw all of it"
        try:
            tx, sig = solana_wallet.signed_transfer(self.seats.kp, owner, lamports, self.seats.rpc.latest_blockhash())
        except Exception as exc:
            return 503, f"not sent (the network gave no blockhash): {str(exc)[:100]}"
        self.last_withdraw = {"sig": sig, "at": time.time()}   # known before sending: a retry can check it first
        try:
            self.seats.rpc.send_transaction(tx)
        except Exception as exc:
            self.state.feed("chief", "error", f"withdrawal tx {sig[:8]}…: the network didn't answer ({str(exc)[:60]}): "
                                              "it may still land, check the balance before trying again"[:160])
            return 503, (f"the network didn't answer (tx {sig[:8]}…): it may still land. Check the bot wallet's balance in a "
                         "minute before trying again")
        status = None
        for _ in range(10):                                   # up to ~20 seconds for the network to confirm it
            try:
                status = self.seats.rpc.signature_status(sig)
            except Exception:
                status = None
            if status and status != "processed":
                break
            self._sleep(2)
        got = lamports / solana_wallet.LAMPORTS_PER_SOL
        failed = str(status).startswith("failed")
        if failed:
            self.last_withdraw = None
        line = (f"WITHDREW {got:.4f} SOL to your wallet {owner[:4]}…{owner[-4:]}: "
                + ("confirmed" if status in ("confirmed", "finalized") else status or "sent, not confirmed yet")
                + f" (tx {sig[:8]}…)")
        self.state.feed("chief", "error" if failed else "sell", line[:160])
        self.read_bot_sol(force=True)
        return (502 if failed else 200), line

    def _sleep(self, s):
        time.sleep(s)

    def money_status(self):
        owner, locked = self.owner_wallet()
        sol = self.bot_sol                                    # read by the loop once a minute, never by the page
        held_real = [p for p in self.state.positions() if p.get("mode") == "live"]
        return {"mode": self.money_mode(), "switching_to": self.money_request,
                "bot_wallet": self.seats.kp.address if self.seats.kp else None, "bot_sol": sol,
                "bot_sol_error": self.bot_sol_err, "owner_wallet": owner, "owner_locked": locked,
                "owner_message": self.owner_message("{address}") if self.seats.kp else None,
                "problems": self.real_problems(check_balance=False), "real_max_ticket_usd": self.cfg.real_max_ticket_usd,
                "max_daily_loss_pct": self.cfg.max_daily_loss_pct, "fee_reserve_sol": self.cfg.fee_reserve_sol,
                "real_coins_held": len(held_real), "fast_lane": self.sniper.mode()}

    # ---------- THE DIAL: how hard to push today ----------
    def dial(self):
        now = self.clock()
        if self._dial is None or now - self._dial[0] >= 60 or now < self._dial[0]:
            self._dial = (now, self._read_dial(now))
        return self._dial[1]

    def _read_dial(self, now):
        if DIAL == "off":
            return {"value": 1.0, "on": False, "early_min": None, "why": ["off (DIAL=off)"]}
        v, why = 1.0, []
        coins = {}                                         # 1. its own last 24 hours, both bots, each coin once, this money's
        mode = self.seats.money()
        for i, t in enumerate(self.state.data.get("trades") or []):
            if t.get("mode", mode) != mode:
                continue
            c = coins.setdefault(t.get("order_id") or f"row{i}", {"pnl": 0.0, "closed": None})
            c["pnl"] += _f(t.get("pnl_usd")) or 0.0
            if t.get("final") is not False and c["closed"] is None:
                c["closed"] = snipe._ts(t.get("closed_at"))
        recent = [c for c in coins.values() if c["closed"] and 0 <= time.time() - c["closed"] <= 86400]
        if len(recent) >= 4:
            wins = sum(1 for c in recent if c["pnl"] > 0)
            f = 1.0 if wins / len(recent) >= 0.5 else 0.8 if wins / len(recent) >= 0.25 else 0.6
            v *= f
            why.append(f"its last 24h: {wins} of {len(recent)} coins won" + (f" (x{f:g})" if f < 1 else ""))
        else:
            why.append(f"its last 24h: {len(recent)} coin{'s' if len(recent) != 1 else ''} closed, too few to judge")
        d = self.state.data                                # 2. today's losses
        start, lost, limit = _f(d.get("day_start_bank")), -(_f(d.get("realized_today")) or 0.0), self.cfg.max_daily_loss_pct
        if d.get("day") == datetime.now(timezone.utc).date().isoformat() and start and limit > 0 and lost >= start * limit / 200:
            v *= 0.75
            why.append(f"down ${lost:,.2f} today, half the daily loss limit (x0.75)")
        moves = [h for t, h in list(self.sniper.mood) if 0 <= now - t <= 1800]   # 3. the hot coins, the last 30 minutes
        if len(moves) >= 15:                               # only a market that's really falling turns it down: in the
            med = statistics.median(moves)                 # simulation a flat one cut tickets while the bot won 13 of 18
            f = 1.0 if med >= 0 else 0.85 if med >= -10 else 0.7
            v *= f
            why.append(f"the market: the hot coins' median hour {med:+.0f}% ({len(moves)} read)" + (f" (x{f:g})" if f < 1 else ""))
        else:
            why.append("the market: not enough hot coins read yet")
        return {"value": round(max(DIAL_MIN, min(1.0, v)), 2), "on": True, "early_min": DIAL_EARLY_MIN, "why": why}

    def _why_bought(self, order):
        """The desk's reasons, kept on the position: Jev's pick and the checks it passed, with the numbers at the pick."""
        rep, out = getattr(self, "_gate_rep", None) or {}, []
        w, c = order.get("worth_trading_at_all"), order.get("confidence")
        out.append("Jev's pick" + (f": worth trading {w:.2f}" if isinstance(w, (int, float)) else "")
                   + (f", confidence {c:.2f}" if isinstance(c, (int, float)) else ", the only coin left") + f"; ticket x{order.get('size_factor')}")
        if rep:
            good = [p["label"] for p in sorted(rep.get("points") or [], key=lambda p: -p["pts"]) if p["pts"] > 0][:3]
            ch = (rep.get("facts") or {}).get("chg") or {}
            out.append(f"safety checks score {rep.get('score')}/100" + (": " + "; ".join(good) if good else ""))
            out.append("at the pick: " + ", ".join(f"{k} {v:+.0f}%" for k, v in (("5m", ch.get("m5")), ("1h", ch.get("h1")), ("6h", ch.get("h6")))
                                                    if isinstance(v, (int, float))))
        return out

    def slots_free(self):
        return len(self.state.positions()) < self.cfg.max_positions

    def held_counts(self):
        """(all coins held, the desk's, the sniper's). A coin from before the split counts as the desk's."""
        ps = self.state.positions()
        snipe = sum(1 for p in ps if p.get("plan") == "snipe")
        return len(ps), len(ps) - snipe, snipe

    def desk_slots_free(self):
        total, desk, _ = self.held_counts()
        waiting = len(self.pending_desk)                  # a pick waiting for its retrace holds its slot
        return total + waiting < self.cfg.max_positions and desk + waiting < self.cfg.desk_slots

    # ---------- the desk's entry: wait for a retrace and a bounce ----------
    def wait_entry(self, order, rep, now):
        tok, F = order["token"], (rep or {}).get("facts") or {}
        price = _f(F.get("price")) or _f((order.get("facts") or {}).get("price_usd"))
        if not price:
            return False
        self.pending_desk[tok["address"]] = {"order": order, "at": now, "price0": price, "high": price, "low": price,
                                             "mcap0": _f(F.get("mcap")) or _f((order.get("facts") or {}).get("mcap_usd")), "ticker": tok.get("ticker"), "last": None, "strong": 0}
        if not self.state.positions():
            book.release()                               # nothing bought yet: taken again when it's bought
        self.state.feed("chief", "info", f"picked at MC {snipe.money(self.pending_desk[tok['address']]['mcap0'])}: waiting up to {DESK_RETRACE_MINUTES:g} min for a "
                        f"{DESK_RETRACE_PCT:g}%+ pullback and a bounce before buying (it doesn't buy the top)", tok.get("ticker"), tok["address"])
        return True

    def entries_due(self, now):
        return bool(self.pending_desk) and (self.last_entry_check is None or now - self.last_entry_check >= 20)

    def run_entries(self, now):
        self.last_entry_check = now
        mints = list(self.pending_desk)[:30]
        try:
            rows = snipe.dex_rows(mints)
        except collect.FetchFailed:
            return "entry: no price right now"
        did = "entry: waiting"
        for m in mints:
            p = self.pending_desk[m]
            F = snipe.pair_facts(snipe.pairs_for(rows, m))
            price, age_min = _f(F.get("price")), (now - p["at"]) / 60
            if price:
                p["high"], p["low"] = max(p["high"], price), min(p["low"], price)
                chg, b5, s5 = F.get("chg") or {}, (F.get("buys") or {}).get("m5"), (F.get("sells") or {}).get("m5")
                m5, h6 = chg.get("m5"), chg.get("h6")
                pull, drop = (1 - price / p["high"]) * 100, (1 - price / p["price0"]) * 100
                p["last"] = {"pull": round(pull, 1), "vs_pick": round(-drop, 1), "mcap": F.get("mcap"), "m5": m5, "h6": h6}
                if drop >= DESK_RETRACE_MAX_DROP:
                    did = self._drop_entry(m, now, f"it kept falling: {-drop:+.0f}% since it was picked, no entry")
                    continue
                if (pull >= DESK_RETRACE_PCT and isinstance(m5, (int, float)) and 0 < m5 < (DESK_MAX_M5_PCT or 1e9)
                        and b5 is not None and s5 is not None and b5 >= s5 * max(1.0, DESK_MIN_BUYERS_X)
                        and b5 >= max(1, DESK_MIN_BUYS_5M) and (h6 is None or h6 >= 0)):
                    did = self._buy_entry(m, now, f"bought the retrace: {pull:.0f}% under its high since the pick, bouncing "
                                                  f"({m5:+.1f}% in 5 min, {b5} buys vs {s5} sells), at MC {snipe.money(F.get('mcap'))}")
                    continue
                strong = self._breakout(F, p, price)
                p["strong"] = p["strong"] + 1 if strong else 0
                if strong and p["strong"] >= 2:                  # held on two checks in a row: not a one-candle wick
                    did = self._buy_entry(m, now, f"bought the breakout: no pullback came, it ran {-drop:+.0f}% since the pick and the "
                                                  f"buyers took over ({strong}), at MC {snipe.money(F.get('mcap'))}")
                    continue
                if strong:
                    did = "entry: breakout building"
            if age_min >= DESK_RETRACE_MINUTES:
                ran = price and price > p["price0"]
                did = self._drop_entry(m, now, f"no clean entry in {DESK_RETRACE_MINUTES:g} min"
                                          + (f": it drifted {(price / p['price0'] - 1) * 100:+.0f}% up without a pullback or a breakout with "
                                             "the buyers heavily ahead, and it doesn't chase" if ran
                                             else ": it never bounced with the buyers ahead in an uptrend"))
        self.snapshot()
        return did

    def _breakout(self, F, p, price):
        """Momentum shifting up hard without a pullback. Returns what it saw, or None."""
        if DESK_BREAKOUT_PCT <= 0:
            return None
        chg, vol = F.get("chg") or {}, F.get("vol") or {}
        b5, s5 = (F.get("buys") or {}).get("m5"), (F.get("sells") or {}).get("m5")
        m5, h1, h6, v5, vh = chg.get("m5"), chg.get("h1"), chg.get("h6"), _f(vol.get("m5")), _f(vol.get("h1"))
        rise, pull = (price / p["price0"] - 1) * 100, (1 - price / p["high"]) * 100
        spike = DESK_MAX_M5_PCT or (self.cfg.snipe_max_m5_pct if self.cfg.snipe_max_m5_pct > 0 else 30)
        if (rise < DESK_BREAKOUT_PCT or pull > 3 or not isinstance(m5, (int, float)) or not 3 <= m5 < spike
                or not isinstance(h1, (int, float)) or h1 <= 0 or (isinstance(h6, (int, float)) and h6 < 0)
                or b5 is None or s5 is None or b5 < 15 or b5 < 1.5 * s5 or not v5 or not vh or v5 < 1.5 * vh / 12):
            return None
        return f"{m5:+.1f}% in 5 min, {b5} buys vs {s5} sells, volume {v5 / (vh / 12):.1f}x its hourly pace"

    def _buy_entry(self, mint, now, why):
        p = self.pending_desk.pop(mint)
        order = p["order"]
        order["why_bought"] = list(order.get("why_bought") or []) + [why]
        total, desk, _ = self.held_counts()
        if total >= self.cfg.max_positions or desk >= self.cfg.desk_slots or mint in {x.get("mint") for x in self.state.positions()}:
            self.pending_desk[mint] = p                   # back in line until the wait runs out
            return self._drop_entry(mint, now, "the slots filled up while it waited (a coin you bought)")
        bank = self.last_bank = self.seats.bank()
        if bank is None or not self.seats.daily_guard(bank):
            self.seats.release_unfilled(order, "no bank or the daily loss limit")
            return "entry: not bought"
        book.take(order)                                 # the desk holds it from here, as with any desk buy
        try:
            self.seats.send_to_seats(order, bank)
        finally:
            self.state.set_working(None)
        self.last_risk = self.last_watch = now
        self.state.feed("chief", "pick", why[:180], p.get("ticker"), mint)
        return "entry: bought"

    def _drop_entry(self, mint, now, why):
        p = self.pending_desk.pop(mint)
        order = p["order"]
        book.sit(f"{mint}:{order['token'].get('network_id') or collect.FOMO_NET['solana']}", "sniper_gate")
        self.seats.release_unfilled(order, why)
        self.state.feed("chief", "cut", f"desk pick dropped: {why}"[:180], p.get("ticker"), mint)
        if self.last_cycle is not None:                  # the slot is free again: the next pick in a minute, not five
            self.last_cycle = min(self.last_cycle, now - max(0.0, CYCLE_SECONDS - GATE_RETRY_SECONDS))
        return "entry: dropped"

    def snipe_slots_free(self, yours=False):
        """The sniper's own slots; a coin you asked for yourself may take any free slot."""
        total, _, snipe = self.held_counts()
        waiting = 0 if yours else len(self.pending_desk)          # a desk pick waiting for its entry keeps its slot
        return total + waiting < self.cfg.max_positions and (yours or snipe < self.cfg.snipe_slots)

    def tick(self):
        """One step of the loop: WATCH and RISK manage every open coin on their clocks, and while a slot is free
        (MAX_POSITIONS) the desk keeps scanning for the next one. Returns the most important thing it did."""
        now = self.clock()
        if self.money_request:                    # FAKE / REAL from the page: switched here, between two steps
            done = self.apply_money()
            if done:
                return done
        if self.test_requested:                   # asked for from the dashboard; runs here, never beside a trade
            mode, self.test_requested = self.test_requested, False
            if mode != self.money_mode():         # asked for on the other money: never runs on this one
                self.state.feed("fills", "info", "test swap cancelled: the money switched since you asked")
                return "test: cancelled"
            return self.run_test_swap()
        if self.sell_requests:                    # a sale you asked for goes first
            return self.run_sell(self.sell_requests.pop(0))
        if self.sniper.queue or self.sniper.forced:   # a coin you pasted (or "buy anyway") goes before anything else
            return self.sniper.run_queued()
        if self.sniper.pending and self.sniper.confirms_due(now) and not self.state.data.get("paused"):
            return self.sniper.run_confirms(now)    # a BUY's second look: one quick call, then buy only if it held up
        done = []                                 # (weight, what): a sale > a scan > a RISK check > a WATCH check
        holding = bool(self.state.positions()) or book.held() is not None
        if holding:
            if self.cfg.watch and (self.last_watch is None or now - self.last_watch >= self.cfg.watch_seconds):
                self.last_watch = now                # WATCH: the price of every coin held, every minute
                pos = self.state.data.get("position") or {}
                self.state.set_working("risk", "watching price", pos.get("ticker"), pos.get("mint"))
                try:
                    trade = self.seats.watch_poll(clock=self.clock)
                finally:
                    self.state.set_working(None)
                done.append((4, ("watch: sold all" if trade.get("final") else "watch: sold part")) if trade else (1, "watch: holding"))
            if self.last_risk is None or now - self.last_risk >= self.cfg.risk_poll_seconds:
                self.last_risk = now
                pos = self.state.data.get("position") or {}
                self.state.set_working("risk", "checking volume", pos.get("ticker"), pos.get("mint"))
                try:
                    trade = self.seats.risk_poll()
                finally:
                    self.state.set_working(None)
                done.append((4, "risk: closed") if trade else (2, "risk: holding"))
            if done:
                self.snapshot()
        if self.seats.kp is not None and (self.bot_sol_at is None or time.time() - self.bot_sol_at >= 60):
            self.read_bot_sol()                       # the bot wallet's SOL, for the page's money box
        if self.pending_desk and self.entries_due(now) and not self.state.data.get("paused"):
            done.append((3, self.run_entries(now)))      # the desk's picks waiting for their retrace
        ran_cycle = False
        resting = self.sniper.cooling(plan="desk", streak=DESK_LOSS_STREAK, minutes=DESK_COOLDOWN_MINUTES)
        if resting and self.desk_slots_free() and not self.state.data.get("paused") and \
                (self.last_cycle is None or now - self.last_cycle >= CYCLE_SECONDS):
            self.last_cycle = now                     # resting: no scan until the rest is over
            self.state.agent("chief", f"resting after {resting['why']}: no desk picks until {resting['at']}", None, "resting")
            done.append((1, "desk: resting"))
        elif self.desk_slots_free():
            if self.state.data.get("paused"):
                if not holding:
                    self.state.agent("chief", f"paused: {self.state.data.get('pause_reason') or 'by you'}", None, "paused")
                    done.append((0, "paused"))
            elif self.last_cycle is None or now - self.last_cycle >= CYCLE_SECONDS:
                done.append((3, self.cycle(now)))
                ran_cycle = True
        if not ran_cycle and not self.state.data.get("paused") and self.sniper.due(now):
            # the fast lane, every minute, full slots or not: hot narratives, new launches, promoted coins, your wallets
            done.append((3, self.sniper.round(now)))
        if not done:
            return "holding" if holding else "waiting"
        return max(enumerate(done), key=lambda x: (x[1][0], x[0]))[1][1]

    def cycle(self, now):
        self.last_cycle = now
        bank = self.last_bank = self.seats.bank()
        if bank is not None and not self.seats.daily_guard(bank):
            return "paused"
        collect.RPC_FAILURES.clear()
        self.coin, self.seen_n, self.seen_of = {}, 0, None
        self.state.set_working("crawler", "listing pools")
        try:
            order, stats = main.run_once(self.lister, self.judge, self, bank, shadow=False)
        finally:
            self.state.set_working(None)
        if collect.RPC_FAILURES:                 # a token cut for *_unknown because Solana refused: say so
            stats["rpc_failed"] = len(collect.RPC_FAILURES)
            own = os.environ.get("SOLANA_RPC_URL", "").strip()
            self.state.error(f"Solana RPC refused {len(collect.RPC_FAILURES)} call(s) this cycle, last: "
                             f"{collect.RPC_FAILURES[-1][:200]}. " + ("Your SOLANA_RPC_URL refused it too: a heavy request on a busy "
                             "coin; RugCheck's holder list stood in where it could." if own else
                             "A free key from helius.dev in SOLANA_RPC_URL fixes this."))
        self.report(order, stats)
        if order:
            ok, why = self.gate(order)
            if not ok:
                book.sit(f"{order['token']['address']}:{order['token'].get('network_id') or collect.FOMO_NET['solana']}", "sniper_gate")
                self.seats.release_unfilled(order, f"sniper gate: {why}")
                # the pick is benched; the other coins that passed are still good: look again in a minute, not five
                self.last_cycle = now - max(0.0, CYCLE_SECONDS - GATE_RETRY_SECONDS)
                self.snapshot()
                return "cycle: gate said no"
            order["why_bought"] = self._why_bought(order)
            if self._gate_rep:                           # what it bought into, so a loss gets its review too
                order["entry_ctx"] = snipe.Sniper._fingerprint(self._gate_rep, self._gate_rep.get("facts") or {})
            if DESK_RETRACE_PCT > 0 and self.wait_entry(order, self._gate_rep, now):
                self.snapshot()
                return "cycle: waiting for a retrace"
            try:
                self.seats.send_to_seats(order, bank)
            finally:
                self.state.set_working(None)
            self.last_risk = self.last_watch = now   # first checks one interval after the fill
        self.snapshot()
        return "cycle: order" if order else "cycle: no order"

    def idle_lines(self):
        """Between picks SIZE, FILLS and RISK still say something true: the ticket they would cut, the price Jupiter
        quotes right now, the rule RISK would sell on. Never a made-up figure: unknown stays unknown."""
        bank = self.last_bank
        if True:
            if _f(bank) is not None:
                top = bank * main.TICKET_SHARE * thresholds.NO_SOCIAL_CUT
                self.state.agent("size", f"ready: next ticket up to ${top:,.2f} = 6% of ${bank:,.0f} x {thresholds.NO_SOCIAL_CUT} "
                                         f"(no X read); never under ${self.cfg.min_ticket_usd:g} or over 2% of the pool",
                                 top, "ticket")
            else:
                self.state.agent("size", "bank unknown: no ticket can be sized", None, "ticket")
            sol = self.seats.sol_usd()
            mode = "LIVE" if self.cfg.live else "paper"
            self.state.agent("fills", f"{mode}: Jupiter quoting, 1 SOL = ${sol:,.2f}" if sol is not None else
                             f"{mode}: Jupiter not answering right now (see errors)", sol, "1 SOL $")
            trades = self.state.data.get("trades") or []
            w, l = seats_record(trades)
            today = _f(self.state.data.get("realized_today")) or 0.0
            if self.state.positions():
                return
            self.state.agent("risk", f"flat. Sells a position when volume ratio < 0.20; stops the day at "
                                     f"-{self.cfg.max_daily_loss_pct:g}% (today {today:+,.2f} USD)", None, f"{w}W {l}L")

    # ---------- dashboard requests (admin token checked by the handler) ----------
    def request_scan(self):
        """'Scan now': the next tick runs a cycle instead of waiting out CYCLE_SECONDS."""
        now = self.clock()
        if not self.desk_slots_free():
            n = self.cfg.max_positions
            return 409, (f"the desk's slots are full: the next scan starts when RISK sells one" if n > 1
                         else "holding a position: no scan until RISK closes it (MAX_POSITIONS=1)")
        if self.state.data.get("paused"):
            return 409, "the desk is paused: tap Resume first"
        if self.last_cycle is not None and now - self.last_cycle < SCAN_NOW_COOLDOWN:
            return 429, f"a scan started {int(now - self.last_cycle)}s ago: wait a minute"
        self.last_cycle = None
        self.state.feed("chief", "info", "scan requested from the dashboard")
        return 200, "scan starts within 5 seconds"

    def request_test(self):
        self.test_requested = self.money_mode()      # remembered with the money it was asked on
        return 200, ("test swap queued: real ~$%.2f SOL -> USDC -> SOL" % TEST_SWAP_USD if self.cfg.live
                     else "test queued: paper mode, quotes only")

    def run_test_swap(self):
        self.state.set_working("fills", "test swap")
        try:
            res = self.seats.test_swap(TEST_SWAP_USD)
        except Exception as exc:
            res = {"time": _now(), "mode": "live" if self.cfg.live else "paper", "usd": TEST_SWAP_USD, "ok": False,
                   "sigs": [], "note": f"not sent: {exc}"}
        finally:
            self.state.set_working(None)
        line = (f"test swap {'OK' if res['ok'] else 'FAILED'} ({res['mode']}): "
                + (f"{res['sol_in']:.5f} SOL -> {res['usdc']:.4f} USDC -> {res['sol_back']:.5f} SOL"
                   if res.get("usdc") is not None and res.get("sol_back") is not None else res["note"]))
        self.state.feed("fills", "test" if res["ok"] else "error", line)
        self.state.agent("fills", line, None, "test")
        with self.state.lock:
            self.state.data["tests"] = ([res] + (self.state.data.get("tests") or []))[:10]
        self.state.save()
        return "test: ok" if res["ok"] else "test: failed"

    # the desk interface main.run_once expects
    def read_x(self, handle):
        return None                              # no X lookups configured: smaller ticket, never a substitute

    def log_shadow(self, order, stats):
        with self.state.lock:
            self.state.data["shadow"] = ([order] + self.state.data["shadow"])[:20]

    def report(self, order, stats):
        kills = {k: sum((stats.get(k) or {}).values()) for k in ("free", "trade", "chain", "soft")}
        with self.state.lock:
            self.state.data["cycle"] = {"time": _now(), "stats": stats, "kills": kills, "why": why(stats),
                                        "order": order["token"] if order else None}
        if stats.get("stood_down"):
            self.state.agent("chief", f"stood down: {stats['stood_down']}", None, "no order")
        elif order is None:
            self.state.agent("chief", f"no order: {stats.get('no_order') or 'nothing survived the cuts'}", None, "no order")
        else:
            t = order["token"]
            unsure = next((c for c in order.get("size_cuts") or [] if c.startswith("no clear favourite")), None)
            self.state.agent("chief", f"pick {t.get('ticker') or t['address'][:6]} on {t['chain']}"
                             + (f": {unsure}, its top choice at half the ticket" if unsure else ""),
                             order.get("confidence"), "confidence")
        self.state.agent("crawler", f"{stats.get('seen', 0)} seen, {stats.get('benched', 0)} benched · "
                                    + (why(stats) or "nothing cut"), stats.get("seen"), "seen")
        self.state.bump("scans")
        if order is None:
            self.idle_lines()
        self.state.feed("chief", "info", f"scan done: {stats.get('seen', 0)} coins, "
                        + ("stood down: " + str(stats["stood_down"])[:80] if stats.get("stood_down")
                           else "order placed" if order else f"no order ({stats.get('no_order') or 'nothing survived'})"))
        self.state.save()

    def snapshot(self):
        """One real point on the balance curve: free cash + the value of every coin held. Skipped if any is unknown."""
        cash = self.seats.bank()
        value = 0.0
        mode = "live" if self.cfg.live else "paper"
        for pos in self.state.positions():
            if pos.get("mode", mode) != mode:     # the other money's coins aren't in this money's balance
                continue
            v = _f(pos.get("last_value_usd"))
            if v is None and pos.get("last_check") is None and pos.get("watched_at") is None:
                v = _f(pos.get("cost_usd"))      # just bought: worth its cost
            if v is None:
                return
            value += v
        if cash is None:
            return
        with self.state.lock:
            self.state.data["equity"] = (self.state.data["equity"] + [[_now(), round(cash + value, 2)]])[-2000:]
            self.state.save()

    def status(self):
        with self.state.lock:
            d = json.loads(json.dumps(self.state.data, default=str))
        d.update(mode="live" if self.cfg.live else "paper", wallet=self.seats.kp.address if self.seats.kp else None,
                 now=_now(), held=book.held(), nets=list(NETS), cycle_seconds=CYCLE_SECONDS,
                 risk_poll_seconds=self.cfg.risk_poll_seconds, goal_usd=_f(d.get("goal_usd")) or _goal(),
                 bank_usd=self.seats.bank() if not self.cfg.live else self.last_bank,   # paper: arithmetic, no network
                 thresholds={"HARD": thresholds.HARD, "SOFT": {k: list(v) for k, v in thresholds.SOFT.items()},
                             "PICK_MIN_WORTH": thresholds.PICK_MIN_WORTH, "PICK_MIN_CONF": thresholds.PICK_MIN_CONF,
                             "DARK_TICKET_CUT": thresholds.DARK_TICKET_CUT, "NO_SOCIAL_CUT": thresholds.NO_SOCIAL_CUT})
        holding = bool(d.get("positions"))
        full = len(d.get("positions") or []) >= self.cfg.max_positions
        total, desk_n, snipe_n = self.held_counts()
        d.update(slots={"desk": self.cfg.desk_slots, "sniper": self.cfg.snipe_slots, "desk_held": desk_n, "sniper_held": snipe_n,
                        "desk_free": self.desk_slots_free(), "sniper_free": self.snipe_slots_free()},
                 desk_gate={"on": DESK_GATE == "on", "score": DESK_GATE_SCORE})
        d["dial"] = self.dial()
        d["money"] = self.money_status()
        d.update(max_positions=self.cfg.max_positions, max_exposure_pct=self.cfg.max_exposure_pct,
                 next_cycle_at=None if full or d.get("paused") else
                 _iso(self.clock() if self.last_cycle is None else self.last_cycle + CYCLE_SECONDS),
                 next_risk_at=_iso(self.last_risk + self.cfg.risk_poll_seconds) if holding and self.last_risk else None,
                 server_now=_iso(self.clock()), test_swap_usd=TEST_SWAP_USD, title=DESK_TITLE, tagline=DESK_TAGLINE, fomo_link=FOMO_LINK,
                 start_bank=self.cfg.paper_bank_usd if not self.cfg.live else None, overrides=OVERRIDES,
                 practice=PRACTICE["active"], practice_note=PRACTICE["note"], watch=self.cfg.watch_plan(),
                 max_daily_loss_pct=self.cfg.max_daily_loss_pct,
                 thresholds_extra={"SHAPE_MIN_CROWD": thresholds.SHAPE_MIN_CROWD},
                 sniper=self.sniper.status(), scorecard=snipe.scorecard(d.get("trades")), storage=STORAGE,
                 desk_waiting=[{"mint": m, "ticker": p.get("ticker"), "mcap0": p.get("mcap0"), "minutes_left":
                                round(max(0.0, DESK_RETRACE_MINUTES - (self.clock() - p["at"]) / 60), 1), "last": p.get("last")}
                               for m, p in self.pending_desk.items()],
                 desk_resting=self.sniper.cooling(plan="desk", streak=DESK_LOSS_STREAK, minutes=DESK_COOLDOWN_MINUTES),
                 desk_retrace={"pct": DESK_RETRACE_PCT, "minutes": DESK_RETRACE_MINUTES, "max_drop": DESK_RETRACE_MAX_DROP,
                               "breakout_pct": DESK_BREAKOUT_PCT},
                 desk_sources={"trending": DESK_TRENDING, "new_pool_pages": DESK_NEW_POOL_PAGES, "metas": DESK_METAS,
                               "min_age_minutes": thresholds.HARD.get("min_age_minutes")})
        d.pop("shadow", None)
        d.pop("wallet_sigs", None)
        return d

    # ---------- the sniper, from the page (admin token checked by the handler) ----------
    def request_check(self, text, buy):
        return self.sniper.request(text, buy)

    def request_check_buy(self, rid):
        return self.sniper.request_buy(str(rid or ""))

    def set_snipe_mode(self, mode):
        return self.sniper.set_mode(str(mode or "").strip().lower())

    def edit_wallets(self, body):
        return self.sniper.edit_wallets(body if isinstance(body, dict) else {})

    def request_sell(self, mint):
        pos = next((p for p in self.state.positions() if p.get("mint") == mint), None)
        if pos is None:
            return 404, "that coin isn't held"
        if mint in self.sell_requests:
            return 409, "already selling it"
        self.sell_requests.append(mint)
        return 200, f"selling ${pos.get('ticker') or mint[:6]} within 5 seconds"

    def run_sell(self, mint):
        pos = next((p for p in self.state.positions() if p.get("mint") == mint), None)
        if pos is None:
            return "sell: not held"
        self.state.set_working("risk", "selling (you asked)", pos.get("ticker"), mint)
        try:
            trade = self.seats.close(pos, "sold by you from the page")
        finally:
            self.state.set_working(None)
        self.snapshot()
        return "sell: sold" if trade else "sell: waiting for a fair price"

    def set_goal(self, value):
        """The goal on the bar at the top, set from the page. 0 or empty clears it (GOAL_USD then applies)."""
        try:
            g = float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 400, "the goal must be a number of dollars"
        if not math.isfinite(g) or g < 0 or g > 1e12:
            return 400, "the goal must be between $0 and $1T"
        with self.state.lock:
            self.state.data["goal_usd"] = g or None
            self.state.save()
        return 200, (f"goal set: ${g:,.0f}" if g else "goal cleared")

    def set_paused(self, paused, reason=None):
        with self.state.lock:
            self.state.data["paused"], self.state.data["pause_reason"] = paused, (reason if paused else None)
            self.state.save()


def _goal():
    try:
        g = float(os.environ.get("GOAL_USD", ""))
        return g if math.isfinite(g) and g > 0 else None
    except ValueError:
        return None


def _fmt(v):
    v = _f(v)
    return "unknown" if v is None else f"{v:.2f}"


# ---------- the dashboard server ----------
def make_handler(runner, admin_token):
    page = (HERE / "dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.path = self.path.split("?", 1)[0]          # ?anything is ignored (cache-busting links still work)
            if self.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/status":
                return self._send(200, runner.status())
            if self.path == "/health":
                return self._send(200, {"ok": True})
            return self._send(404, {"detail": "not found"})

        def do_POST(self):
            if self.path not in ("/api/pause", "/api/resume", "/api/scan-now", "/api/test-swap", "/api/goal",
                                 "/api/check", "/api/check-buy", "/api/snipe-mode", "/api/wallets", "/api/sell",
                                 "/api/money", "/api/owner", "/api/fund", "/api/withdraw"):
                return self._send(404, {"detail": "not found"})
            given = self.headers.get("X-Admin-Token", "")
            if len(admin_token) < MIN_ADMIN_TOKEN or not hmac.compare_digest(given.encode(), admin_token.encode()):
                return self._send(401, {"detail": "wrong or missing admin token"})
            if self.path == "/api/scan-now":
                code, msg = runner.request_scan()
                return self._send(code, {"detail": msg})
            if self.path == "/api/test-swap":
                code, msg = runner.request_test()
                return self._send(code, {"detail": msg})
            body = None
            if self.path in ("/api/goal", "/api/check", "/api/check-buy", "/api/snipe-mode", "/api/wallets", "/api/sell",
                             "/api/money", "/api/owner", "/api/fund", "/api/withdraw"):
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 2000)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, OSError):
                    body = None
                if not isinstance(body, dict):
                    return self._send(400, {"detail": "send a JSON object"})
            if self.path == "/api/goal":
                code, msg = runner.set_goal(body.get("goal_usd"))
                return self._send(code, {"detail": msg})
            if self.path == "/api/check":
                code, msg = runner.request_check(body.get("text") or body.get("mint"), body.get("buy") is True)
                return self._send(code, {"detail": msg})
            if self.path == "/api/check-buy":
                code, msg = runner.request_check_buy(body.get("id"))
                return self._send(code, {"detail": msg})
            if self.path == "/api/snipe-mode":
                code, msg = runner.set_snipe_mode(body.get("mode"))
                return self._send(code, {"detail": msg})
            if self.path == "/api/wallets":
                code, msg = runner.edit_wallets(body)
                return self._send(code, {"detail": msg})
            if self.path == "/api/sell":
                code, msg = runner.request_sell(str(body.get("mint") or ""))
                return self._send(code, {"detail": msg})
            if self.path == "/api/money":
                code, msg = runner.set_money(body.get("mode"), body.get("confirm"))
                return self._send(code, {"detail": msg})
            if self.path == "/api/owner":
                code, msg = runner.register_owner(body.get("address"), body.get("signature"), str(body.get("message") or ""))
                return self._send(code, {"detail": msg})
            if self.path == "/api/fund":
                code, out = runner.fund_message(body.get("from"), body.get("sol"))
                return self._send(code, out if isinstance(out, dict) else {"detail": out})
            if self.path == "/api/withdraw":
                code, msg = runner.withdraw(body.get("sol"))
                return self._send(code, {"detail": msg})
            runner.set_paused(self.path == "/api/pause", "paused from the dashboard")
            return self._send(200, {"paused": bool(runner.status().get("paused"))})

    return Handler


def start_desk(state):
    """The desk on localhost. Needs fastapi, uvicorn and typesafe-sdk (requirements.txt) and TYPESAFE_API_KEY."""
    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        state.error("TYPESAFE_API_KEY is not set: the desk can't ask Jev, so every cycle stands down")
        return False
    try:
        import uvicorn

        import judge
    except Exception as exc:
        state.error(f"the desk could not start: {type(exc).__name__}: {exc}")
        return False
    threading.Thread(target=uvicorn.run, args=(judge.app,),
                     kwargs={"host": "127.0.0.1", "port": DESK_PORT, "log_level": "warning"}, daemon=True).start()
    return True


OVERRIDES = {}
PRACTICE = {"active": False, "note": None}
# PRACTICE_MODE=true (paper only): the gates that kept every coin out, opened up so the whole loop can be watched.
# The preset wins over the same variables set by hand (practice is one known setting); other variables still apply.
# Paper results under these numbers say nothing about the real rules. Ignored the moment LIVE_MODE=true.
PRACTICE_PRESET = {"SHAPE_MIN_CROWD": "0.25", "PICK_MIN_WORTH": "0.20", "PICK_MIN_CONF": "0.30",
                   "SOFT_MOMENTUM_ALREADY_SPENT": "0.90", "SOFT_CONCENTRATION_IS_EXIT_RISK": "0.80",
                   "SOFT_DEV_STILL_LOADED": "0.80", "SOFT_LIQUIDITY_FITS_TICKET": "0.35",
                   "HARD_MAX_AGE_HOURS": "168", "HARD_MAX_TOP_WALLET": "0.12", "HARD_MAX_TOP_10": "0.80"}


def practice_env(env):
    """The variables with the practice preset under them (a variable you set yourself still wins)."""
    want = str(env.get("PRACTICE_MODE", "")).strip().lower() == "true"
    live = str(env.get("LIVE_MODE", "")).strip().lower() == "true"
    PRACTICE.update(active=False, note=None)
    if not want:
        return env
    if live:
        PRACTICE["note"] = "PRACTICE_MODE is ignored in live mode: the real rules apply"
        return env
    PRACTICE.update(active=True, note="practice mode: looser gates, paper only")
    return {**dict(env), **PRACTICE_PRESET}


def apply_overrides(env):
    """HARD_<NAME>, SOFT_<NAME>, SHAPE_MIN_CROWD, PICK_MIN_WORTH, PICK_MIN_CONF from the variables, so a threshold can
    be tuned from the phone. Changed in place, where filter.py and pick.py read them. A bad value stops the desk
    and names the variable (never its value)."""
    applied = {}
    for name, raw in env.items():
        key = None
        if name.startswith("HARD_") and name[5:].lower() in thresholds.HARD:
            key = ("HARD", name[5:].lower())
        elif name.startswith("SOFT_") and name[5:].lower() in thresholds.SOFT:
            key = ("SOFT", name[5:].lower())
        elif name in ("SHAPE_MIN_CROWD", "PICK_MIN_WORTH", "PICK_MIN_CONF"):
            key = ("ONE", name)
        if key is None:
            continue
        try:
            v = float(str(raw).strip())
        except ValueError:
            v = math.nan
        if not math.isfinite(v) or v < 0 or (key[0] != "HARD" and v > 10):
            raise SetupProblem(f"{name} must be a number" + ("" if key[0] == "HARD" else " between 0 and 10"))
        if key[0] == "HARD":
            old = thresholds.HARD[key[1]]
            thresholds.HARD[key[1]] = int(v) if isinstance(old, int) and v.is_integer() else v
        elif key[0] == "SOFT":
            thresholds.SOFT[key[1]] = (thresholds.SOFT[key[1]][0], v)
        else:
            for mod in (thresholds, desk_filter, desk_pick):
                if hasattr(mod, name):
                    setattr(mod, name, v)
        applied[name] = v
    OVERRIDES.clear()
    OVERRIDES.update(applied)
    return applied


MIN_TRANSFER_LAMPORTS = 1_000_000                  # 0.001 SOL: under rent, a transfer to an empty wallet fails on chain


def _sol_amount(v):
    """An amount of SOL from the page: a finite positive number, or None."""
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _sig_bytes(sig):
    """A wallet's signature as sent by the page: base64 (Phantom's Uint8Array, encoded) or base58. None if neither."""
    import base64
    s = str(sig or "").strip()
    for dec in (lambda x: base64.b64decode(x, validate=True), b58decode):
        try:
            raw = dec(s)
        except Exception:
            continue
        if len(raw) == 64:
            return raw
    return None


class SetupProblem(Exception):
    """A variable is wrong. The page says which one; nothing scans or trades until it is fixed."""


class Stopped:
    """What the dashboard talks to when the desk could not be built: the reason, and nothing else."""

    def __init__(self, reason):
        self.reason = f"setup problem: {reason}"
        self.started = _now()

    def status(self):
        return {"mode": "stopped", "paused": True, "pause_reason": self.reason, "errors": [{"time": self.started, "text": self.reason}],
                "agents": {}, "equity": [], "trades": [], "judge": [], "position": None, "cycle": None, "wallet": None,
                "nets": list(NETS), "now": _now(), "goal_usd": None}

    def set_paused(self, paused, reason=None):
        pass

    def request_scan(self):
        return 409, self.reason

    def request_test(self):
        return 409, self.reason

    def set_goal(self, value):
        return 409, self.reason

    def request_check(self, text, buy):
        return 409, self.reason

    def request_check_buy(self, rid):
        return 409, self.reason

    def set_snipe_mode(self, mode):
        return 409, self.reason

    def edit_wallets(self, body):
        return 409, self.reason

    def request_sell(self, mint):
        return 409, self.reason

    def set_money(self, mode, confirm=None):
        return 409, self.reason

    def register_owner(self, address, signature, message):
        return 409, self.reason

    def fund_message(self, from_addr, sol):
        return 409, self.reason

    def withdraw(self, sol):
        return 409, self.reason

    def tick(self):
        return "stopped"


def build(env=os.environ):
    if str(env.get("LIVE_MODE", "")).strip().lower() == "true" and env.get("RAILWAY_PROJECT_ID") \
            and not env.get("RAILWAY_VOLUME_MOUNT_PATH"):
        raise SetupProblem("LIVE_MODE=true on Railway needs a volume first: without one, a redeploy forgets the open "
                           "position while the tokens are still in the wallet")
    cfg = Config(env)
    apply_overrides(practice_env(env))
    state = State(os.path.join(DATA_DIR, "state.json"))
    key = env.get("BOT_WALLET_KEY", "").strip()
    kp = Keypair.from_env(key) if key else None
    rpc = RPC([env.get("SOLANA_RPC_URL", "").strip()] if env.get("SOLANA_RPC_URL") else None)
    seats = Seats(cfg, Jupiter(env.get("JUPITER_API_KEY")), rpc, kp, state)
    import judge_client
    return Runner(cfg, seats, state, judge_client.judge, PublicList())


def safe_build(env=os.environ):
    """build(), or a Stopped desk that shows why. A wrong variable must not crash-loop the service: the page has to
    come up and say what to fix. The message never contains a variable's value (BOT_WALLET_KEY above all)."""
    try:
        return build(env)
    except SetupProblem as exc:
        return Stopped(str(exc))
    except WalletKeyError:
        return Stopped("BOT_WALLET_KEY is not a valid Solana private key (Phantom's base58 export, or a JSON array "
                       "of 64 numbers)")
    except ValueError as exc:                     # LIVE_MODE not true/false; LIVE_MODE=true without BOT_WALLET_KEY
        return Stopped(str(exc))


def serve():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    runner = safe_build()
    admin = os.environ.get("ADMIN_TOKEN", "").strip()
    if isinstance(runner, Stopped):
        log.error(runner.reason)
    else:
        if os.environ.get("PAUSED", "").strip().lower() == "true":
            runner.set_paused(True, "PAUSED=true in the variables")
        if PRACTICE["note"] and not PRACTICE["active"]:
            runner.state.error(PRACTICE["note"])
        elif PRACTICE["active"]:
            runner.state.feed("chief", "info", "PRACTICE MODE: looser gates so trades happen; paper only. "
                                               "Results here say nothing about the real rules.")
        if len(admin) < MIN_ADMIN_TOKEN:
            runner.state.error(f"ADMIN_TOKEN is missing or shorter than {MIN_ADMIN_TOKEN} characters: "
                               "the pause button is off")
        start_desk(runner.state)
        time.sleep(3)                             # let the desk bind before the first cycle
    httpd = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), make_handler(runner, admin))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if isinstance(runner, Stopped):
        while True:
            time.sleep(3600)
    log.info("dashboard on port %s, %s mode, wallet %s", httpd.server_address[1], "LIVE" if runner.cfg.live else "paper",
             runner.seats.kp.address if runner.seats.kp else "none")
    while True:
        try:
            runner.tick()
        except Exception as exc:                  # a bad cycle never stops the desk
            runner.state.error(f"loop: {type(exc).__name__}: {exc}")
        time.sleep(5)


if __name__ == "__main__":
    serve()
