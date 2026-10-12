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
MIN_ADMIN_TOKEN = 16
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("DESK_DB", os.path.join(DATA_DIR, "desk.db"))
os.environ.setdefault("JUDGE_LOG", os.path.join(DATA_DIR, "judge_log.jsonl"))
os.environ.setdefault("DESK_SECRET", secrets.token_urlsafe(32))       # internal: bot and desk share this process
DESK_PORT = int(os.environ.get("DESK_PORT", "8081"))
os.environ.setdefault("JUDGE_URL", f"http://127.0.0.1:{DESK_PORT}")

import book  # noqa: E402
import collect  # noqa: E402
import main  # noqa: E402
import thresholds  # noqa: E402
from jupiter import Jupiter  # noqa: E402
from public_list import PublicList  # noqa: E402
from seats import Config, Seats, State, _now  # noqa: E402
from solana_wallet import RPC, Keypair, WalletKeyError  # noqa: E402

log = logging.getLogger("runner")
NETS = tuple(n.strip() for n in os.environ.get("NETS", "solana").split(",") if n.strip())
CYCLE_SECONDS = float(os.environ.get("CYCLE_SECONDS", main.CYCLE_SECONDS))


TRENDING_WINDOW = os.environ.get("TRENDING_WINDOW", "1h").strip()
if TRENDING_WINDOW not in ("5m", "1h", "6h", "24h"):
    TRENDING_WINDOW = "1h"
try:
    TRENDING_PAGES = max(0, min(3, int(os.environ.get("TRENDING_PAGES", "1"))))
except ValueError:
    TRENDING_PAGES = 1


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


class Runner:
    def __init__(self, cfg, seats, state, judge, lister, clock=time.time):
        self.cfg, self.seats, self.state, self._judge, self.lister, self.clock = cfg, seats, state, judge, lister, clock
        self.last_cycle = self.last_risk = None
        self.last_bank = None
        main.universe = universe                  # trade only what Jupiter can, trending first

    # the judge, with every answer kept for the dashboard's agent cards
    def judge(self, set_name, state):
        r = self._judge(set_name, state)
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

    def tick(self):
        """One step of the loop. Returns what it did (for the log and the tests)."""
        now = self.clock()
        holding = self.state.data.get("position") is not None or book.held() is not None
        if holding:
            if self.last_risk is None or now - self.last_risk >= self.cfg.risk_poll_seconds:
                self.last_risk = now
                trade = self.seats.risk_poll()
                self.snapshot()
                return "risk: closed" if trade else "risk: holding"
            return "holding"
        if self.state.data.get("paused"):
            self.state.agent("chief", f"paused: {self.state.data.get('pause_reason') or 'by you'}", None, "paused")
            return "paused"
        if self.last_cycle is not None and now - self.last_cycle < CYCLE_SECONDS:
            return "waiting"
        self.last_cycle = now
        bank = self.last_bank = self.seats.bank()
        if bank is not None and not self.seats.daily_guard(bank):
            return "paused"
        collect.RPC_FAILURES.clear()
        order, stats = main.run_once(self.lister, self.judge, self, bank, shadow=False)
        if collect.RPC_FAILURES:                 # a token cut for *_unknown because Solana refused: say so
            stats["rpc_failed"] = len(collect.RPC_FAILURES)
            self.state.error(f"Solana RPC refused {len(collect.RPC_FAILURES)} call(s) this cycle, last: "
                             f"{collect.RPC_FAILURES[-1][:200]}. A free key from helius.dev in SOLANA_RPC_URL fixes this.")
        self.report(order, stats)
        if order:
            self.seats.send_to_seats(order, bank)
            self.last_risk = now                 # first RISK check one poll interval after the fill
        self.snapshot()
        return "cycle: order" if order else "cycle: no order"

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
        self.state.save()

    def snapshot(self):
        """One real point on the balance curve: free cash + open position value. Skipped if either is unknown."""
        pos = self.state.data.get("position")
        cash = self.seats.bank()
        value = 0.0 if pos is None else _f(pos.get("last_value_usd"))
        if pos is not None and value is None:
            value = _f(pos.get("cost_usd")) if pos.get("last_check") is None else None   # just bought: worth its cost
        if cash is None or value is None:
            return
        with self.state.lock:
            self.state.data["equity"] = (self.state.data["equity"] + [[_now(), round(cash + value, 2)]])[-2000:]
            self.state.save()

    def status(self):
        with self.state.lock:
            d = json.loads(json.dumps(self.state.data, default=str))
        d.update(mode="live" if self.cfg.live else "paper", wallet=self.seats.kp.address if self.seats.kp else None,
                 now=_now(), held=book.held(), nets=list(NETS), cycle_seconds=CYCLE_SECONDS,
                 risk_poll_seconds=self.cfg.risk_poll_seconds, bank_usd=self.last_bank, goal_usd=_goal(),
                 thresholds={"HARD": thresholds.HARD, "SOFT": {k: list(v) for k, v in thresholds.SOFT.items()},
                             "PICK_MIN_WORTH": thresholds.PICK_MIN_WORTH, "PICK_MIN_CONF": thresholds.PICK_MIN_CONF,
                             "DARK_TICKET_CUT": thresholds.DARK_TICKET_CUT, "NO_SOCIAL_CUT": thresholds.NO_SOCIAL_CUT})
        d.pop("shadow", None)
        return d

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
            if self.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/status":
                return self._send(200, runner.status())
            if self.path == "/health":
                return self._send(200, {"ok": True})
            return self._send(404, {"detail": "not found"})

        def do_POST(self):
            if self.path not in ("/api/pause", "/api/resume"):
                return self._send(404, {"detail": "not found"})
            given = self.headers.get("X-Admin-Token", "")
            if len(admin_token) < MIN_ADMIN_TOKEN or not hmac.compare_digest(given.encode(), admin_token.encode()):
                return self._send(401, {"detail": "wrong or missing admin token"})
            runner.set_paused(self.path == "/api/pause", "paused from the dashboard")
            return self._send(200, {"paused": runner.state.data["paused"]})

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

    def tick(self):
        return "stopped"


def build(env=os.environ):
    if str(env.get("LIVE_MODE", "")).strip().lower() == "true" and env.get("RAILWAY_PROJECT_ID") \
            and not env.get("RAILWAY_VOLUME_MOUNT_PATH"):
        raise SetupProblem("LIVE_MODE=true on Railway needs a volume first: without one, a redeploy forgets the open "
                           "position while the tokens are still in the wallet")
    cfg = Config(env)
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
