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
import threading
import time
from datetime import datetime, timezone
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
from solana_wallet import RPC, Keypair, WalletKeyError  # noqa: E402

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


TRENDING_WINDOW = os.environ.get("TRENDING_WINDOW", "1h").strip()
if TRENDING_WINDOW not in ("5m", "1h", "6h", "24h"):
    TRENDING_WINDOW = "1h"
try:
    TRENDING_PAGES = max(0, min(3, int(os.environ.get("TRENDING_PAGES", "1"))))
except ValueError:
    TRENDING_PAGES = 1
try:
    TEST_SWAP_USD = min(20.0, max(0.5, float(os.environ.get("TEST_SWAP_USD", "1"))))
except ValueError:
    TEST_SWAP_USD = 1.0
DESK_GATE = "off" if os.environ.get("DESK_GATE", "on").strip().lower() == "off" else "on"
try:
    DESK_GATE_SCORE = max(0.0, min(100.0, float(os.environ.get("DESK_GATE_SCORE", "25"))))
except ValueError:
    DESK_GATE_SCORE = 25.0
SCAN_NOW_COOLDOWN = 60          # seconds: "Scan now" can't fire twice inside a minute (GeckoTerminal, Jev credits)
GATE_RETRY_SECONDS = 60         # the desk's pick failed the safety checks: the next scan starts this soon


def _f(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def universe(pages):
    """Trending pools first (what is trading now), then main's new pools. One GeckoTerminal slot per page.
    Either list failing is recorded in collect.UNIVERSE_FAILURES; only both failing stands the cycle down."""
    fresh = collect.universe(nets=NETS, pages=pages)          # clears the failure list
    hot = collect.trending(nets=NETS, pages=TRENDING_PAGES, duration=TRENDING_WINDOW) if TRENDING_PAGES else []
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
        main.universe = universe                  # trade only what Jupiter can, trending first
        main.book = SlotBook(self)                # main stands down only when every slot is full
        self._instrument()
        self.sniper = snipe.Sniper(self)          # your own checks, the wallets you follow, the fast lane
        self._watching = False
        self.sell_requests = []                   # coins you asked to sell from the page
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

        def dossier(t):
            step("vet", "dossier", t)
            try:
                d = timed(collect.dossier, t)
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
        if DESK_GATE == "off":
            return True, "gate off"
        # Normal coins: the safety checks only (rugs, copycats, can't sell, already ran and cooling, sellers ahead).
        # Not the sniper's early-coin rules (its age and size window, narrative, hype, 2x room): those are the sniper's.
        rep = self.sniper.check(tok["address"], "desk pick", purpose="desk")
        ratio = (rep.get("flow") or {}).get("ratio15")
        m5 = ((rep.get("facts") or {}).get("chg") or {}).get("m5")
        if rep["verdict"] == "SKIP":
            ok, why = False, "; ".join(rep.get("veto") or [])[:160] or "a rug check failed"
        elif isinstance(m5, (int, float)) and self.cfg.snipe_max_m5_pct > 0 and m5 >= self.cfg.snipe_max_m5_pct:
            ok, why = False, f"it's spiking right now ({m5:+.1f}% in 5 min): not buying the top of the candle"
        elif (isinstance(m5, (int, float)) and m5 <= -8) or (isinstance(ratio, (int, float)) and ratio < 0.7):
            ok, why = False, "it's dumping right now" + (f" ({m5:+.1f}% in 5 min)" if isinstance(m5, (int, float)) and m5 <= -8 else
                                                          f" (sellers {1 / ratio:.1f}x buyers over 15 min)" if ratio else "")
        elif (rep.get("score") or 0) < DESK_GATE_SCORE:
            ok, why = False, f"sniper score {rep.get('score')} is under {DESK_GATE_SCORE:g}"
        else:
            ok, why = True, f"sniper score {rep.get('score')}"
        rep["action"] = ("desk pick: passed the sniper's checks, buying" if ok else f"desk pick not bought: {why}")
        self.sniper._save(rep)
        if not ok:
            self.state.feed("chief", "cut", f"desk pick stopped by the sniper's checks: {why}", tok.get("ticker"), tok["address"])
        return ok, why

    def slots_free(self):
        return len(self.state.positions()) < self.cfg.max_positions

    def held_counts(self):
        """(all coins held, the desk's, the sniper's). A coin from before the split counts as the desk's."""
        ps = self.state.positions()
        snipe = sum(1 for p in ps if p.get("plan") == "snipe")
        return len(ps), len(ps) - snipe, snipe

    def desk_slots_free(self):
        total, desk, _ = self.held_counts()
        return total < self.cfg.max_positions and desk < self.cfg.desk_slots

    def snipe_slots_free(self, yours=False):
        """The sniper's own slots; a coin you asked for yourself may take any free slot."""
        total, _, snipe = self.held_counts()
        return total < self.cfg.max_positions and (yours or snipe < self.cfg.snipe_slots)

    def tick(self):
        """One step of the loop: WATCH and RISK manage every open coin on their clocks, and while a slot is free
        (MAX_POSITIONS) the desk keeps scanning for the next one. Returns the most important thing it did."""
        now = self.clock()
        if self.test_requested:                   # asked for from the dashboard; runs here, never beside a trade
            self.test_requested = False
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
        ran_cycle = False
        if self.desk_slots_free():
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
            self.state.error(f"Solana RPC refused {len(collect.RPC_FAILURES)} call(s) this cycle, last: "
                             f"{collect.RPC_FAILURES[-1][:200]}. A free key from helius.dev in SOLANA_RPC_URL fixes this.")
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
        self.test_requested = True
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
            self.state.agent("chief", f"pick {t.get('ticker') or t['address'][:6]} on {t['chain']}",
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
        for pos in self.state.positions():
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
        d.update(max_positions=self.cfg.max_positions, max_exposure_pct=self.cfg.max_exposure_pct,
                 next_cycle_at=None if full or d.get("paused") else
                 _iso(self.clock() if self.last_cycle is None else self.last_cycle + CYCLE_SECONDS),
                 next_risk_at=_iso(self.last_risk + self.cfg.risk_poll_seconds) if holding and self.last_risk else None,
                 server_now=_iso(self.clock()), test_swap_usd=TEST_SWAP_USD, title=DESK_TITLE, tagline=DESK_TAGLINE, fomo_link=FOMO_LINK,
                 start_bank=self.cfg.paper_bank_usd if not self.cfg.live else None, overrides=OVERRIDES,
                 practice=PRACTICE["active"], practice_note=PRACTICE["note"], watch=self.cfg.watch_plan(),
                 max_daily_loss_pct=self.cfg.max_daily_loss_pct,
                 thresholds_extra={"SHAPE_MIN_CROWD": thresholds.SHAPE_MIN_CROWD},
                 sniper=self.sniper.status(), scorecard=snipe.scorecard(d.get("trades")), storage=STORAGE)
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
                                 "/api/check", "/api/check-buy", "/api/snipe-mode", "/api/wallets", "/api/sell"):
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
            if self.path in ("/api/goal", "/api/check", "/api/check-buy", "/api/snipe-mode", "/api/wallets", "/api/sell"):
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
