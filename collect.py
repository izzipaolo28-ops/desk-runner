# collect.py
"""Everything the desk reads, and the exact state each question set sees.

    universe -> shortlist (FOMO) -> trade_counts (DexScreener) -> dossier (GeckoTerminal + Solana RPC)
    market_state / chain_state / social_state: the named, minimal object each set gets

Rules kept here:
  * Never invent a number. A field that came back null stays null, and the question sees the null.
  * A token a cut killed never spends another request: trade_counts() needs filter.free_kill's stamp
    (t["stage2"] == "pass") and dossier() needs both stamps (stage2 and stage3), else KilledToken.
  * Errors, as the desk runbook says:
      GeckoTerminal 429  -> pause every GeckoTerminal call for a full minute; this call fails (no retry in place)
      DexScreener 429    -> trade_data "rate_limited"; main stops pass two for the cycle (lower DEX_BUDGET)
      dossier failure    -> DossierFailed; main skips the token (not a pass, not a retry loop)
      GeckoTerminal 404  -> not indexed yet: real nulls, normal on Robinhood (data_coverage handles it)
"""
import logging
import math
import os
import re
import threading
import time
import urllib.parse
from collections import deque

import requests

try:
    from fomo_api import Fomo                  # Privy bearer out of Chrome over CDP
except ImportError:                            # lets this module be imported and tested without it
    Fomo = None

log = logging.getLogger(__name__)

GT_KEY = os.environ.get("COINGECKO_DEMO_API_KEY", "").strip() or None
GT = "https://api.coingecko.com/api/v3/onchain" if GT_KEY else "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com/tokens/v1"            # chain-scoped token endpoint, documented
# Your own RPC first (SOLANA_RPC_URL, e.g. a free Helius key), then public endpoints. Public ones rate-limit and
# some refuse getTokenLargestAccounts, so one refusing is not the answer: the next one is asked.
RPC_FALLBACKS = ("https://solana-rpc.publicnode.com", "https://solana.drpc.org",
                 "https://api.mainnet.solana.com", "https://api.mainnet-beta.solana.com")
RPC_FAILURES: list[str] = []      # calls every endpoint refused, newest last, for the dashboard. Hosts only:
_rpc_last_good = [None]           # an RPC URL can carry an API key and is never logged whole


def _rpc_urls():
    own = os.environ.get("SOLANA_RPC_URL", "").strip()
    urls = ([own] if own else []) + [u for u in RPC_FALLBACKS if u != own]
    good = _rpc_last_good[0]
    return ([good] + [u for u in urls if u != good]) if good in urls else urls


def _host(url):
    return urllib.parse.urlsplit(url).hostname or "rpc"

GT_NET = {1399811149: "solana", 4663: "robinhood", 56: "bsc", 8453: "base"}
FOMO_NET = {v: k for k, v in GT_NET.items()}
DEX_CHAIN = {"solana": "solana", "bsc": "bsc", "base": "base", "robinhood": "robinhood"}  # robinhood: unconfirmed
# Each chain gets the question set written for its own evidence. Base shares BSC's. Routing lives here,
# once: main (which set to ask), filter (which facts apply) and pick (what the summary says) all read it.
CHAIN_SET = {1399811149: "solana", 56: "bsc", 8453: "bsc", 4663: "robinhood"}
MCAP_UNKNOWN = 9223372036854775806             # Codex: "could not be computed"
X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
HTTP = requests.Session()
HTTP.headers.update({"Accept": "application/json", "User-Agent": "desk-pipeline/1"})


class KilledToken(ValueError):
    """A later stage was asked to spend a request on a token an earlier cut killed (a caller bug)."""


class DossierFailed(RuntimeError):
    """The dossier could not be read (rate limit, network, bad body). Not a 404: that means 'not indexed'."""


class FetchFailed(RuntimeError):
    def __init__(self, url, why, status=None):
        super().__init__(f"{why}: {url}")
        self.status = status


# ---- plumbing -------------------------------------------------------------------------------------
class _Limiter:
    """Sliding 60 s window shared by every GeckoTerminal call, plus a hard pause after a 429."""

    def __init__(self, per_minute):
        self.per_minute, self.sent, self.lock, self.paused_until = per_minute, deque(), threading.Lock(), 0.0

    def pause(self, seconds):
        with self.lock:
            self.paused_until = max(self.paused_until, time.monotonic() + seconds)

    def wait(self):
        with self.lock:
            while True:
                now = time.monotonic()
                if now < self.paused_until:
                    time.sleep(self.paused_until - now)
                    continue
                while self.sent and now - self.sent[0] >= 60:
                    self.sent.popleft()
                if len(self.sent) < self.per_minute:
                    self.sent.append(now)
                    return
                time.sleep(60 - (now - self.sent[0]))


GT_LIMIT = _Limiter(int(os.environ.get("GT_MAX_CALLS_PER_MIN", 90 if GT_KEY else 8)))


def _get_json(url, params=None, limiter=None, tries=2):
    """GET -> parsed JSON. 404 -> None (nothing there). Everything else that fails raises FetchFailed.
    A 429 is never retried in place: GeckoTerminal pauses its limiter for a full minute, DexScreener
    just fails the call."""
    headers = {"x-cg-demo-api-key": GT_KEY} if GT_KEY and url.startswith(GT) else None
    last = "no attempt"
    for attempt in range(tries):
        if limiter:
            limiter.wait()
        try:
            r = HTTP.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            last = f"network error ({type(exc).__name__})"
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            if limiter is GT_LIMIT:
                GT_LIMIT.pause(60)
            raise FetchFailed(url, "rate limited (429)", 429)
        if r.status_code in (500, 502, 503, 504):
            last = f"HTTP {r.status_code}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code != 200:
            raise FetchFailed(url, f"HTTP {r.status_code}", r.status_code)
        try:
            return r.json()
        except ValueError:
            raise FetchFailed(url, "not JSON") from None
    raise FetchFailed(url, last)


def _num(v):
    """'123.4' / 123.4 -> 123.4; None, junk, NaN, inf, bools -> None. Codex sends many numbers as strings."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _require(t, *stages):
    for stage in stages:
        if t.get(stage) != "pass":
            raise KilledToken(f"{t.get('tid') or t.get('addr')}: {stage} is {t.get(stage) or 'not run'}, "
                              f"so it gets no further requests")


# ---- pass one: universe + FOMO list ----------------------------------------------------------------
def age_minutes(created):
    """createdAt comes back as epoch seconds or milliseconds depending on the row. Unknown is None, never
    0.0. More than two minutes in the future is bad data (None); within two minutes it is clock skew."""
    c = _num(created)
    if not c or c <= 0:
        return None
    if c > 1e11:
        c /= 1000
    minutes = (time.time() - c) / 60
    return None if minutes < -2 else max(0.0, minutes)


UNIVERSE_FAILURES: list[str] = []   # what the last universe() could not fetch. An empty list from a dead
                                    # GeckoTerminal is not "no new pools", and the cycle must say which it was


def universe(nets=("solana", "bsc", "robinhood"), pages=2) -> list[str]:
    """Fresh pools per chain -> ['<addr>:<netId>', ...]. One GeckoTerminal slot per chain per page.
    If GeckoTerminal keeps answering 429, run with pages=1: six slots become three."""
    UNIVERSE_FAILURES.clear()
    return _pool_list("new_pools", {}, nets, pages)


def trending(nets=("solana",), pages=1, duration="1h") -> list[str]:
    """GeckoTerminal's trending pools: what is trading now. new_pools alone is mostly minutes-old launches that
    the age and volume floors cut before they can qualify; trending is where 15-minute-to-72-hour tokens with
    real volume show up. Same ids, same slots (one per chain per page). Failures add to UNIVERSE_FAILURES."""
    return _pool_list("trending_pools", {"duration": duration}, nets, pages)


def _pool_list(kind, params, nets, pages):
    ids, seen = [], set()
    for net in nets:
        if net not in FOMO_NET:
            log.warning("universe: no FOMO netId for %r", net)
            UNIVERSE_FAILURES.append(f"{net}: no FOMO netId")
            continue
        for page in range(1, pages + 1):
            try:
                r = _get_json(f"{GT}/networks/{net}/{kind}", {**params, "page": page}, GT_LIMIT)
            except FetchFailed as exc:
                log.warning("universe: %s %s page %d: %s", net, kind, page, exc)
                UNIVERSE_FAILURES.append(f"{net} {kind} page {page}: {exc}")
                break
            if not isinstance(r, dict) or not isinstance(r.get("data"), list):
                log.warning("universe: %s %s page %d unavailable", net, kind, page)
                UNIVERSE_FAILURES.append(f"{net} {kind} page {page}: no pool list in the answer")
                break
            for pool in r["data"]:
                base = ((pool.get("relationships") or {}).get("base_token") or {})
                gid = (base.get("data") or {}).get("id") or ""
                prefix = f"{net}_"
                if not gid.startswith(prefix):
                    continue
                tid = f"{gid[len(prefix):]}:{FOMO_NET[net]}"
                if tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
    return ids


def normalise(tid: str, m: dict) -> dict:
    """FOMO's field names become the desk's field names, once, here. Every number goes through _num."""
    addr, net = tid.rsplit(":", 1)
    mcap = _num(m.get("mcap"))
    change = m.get("change") or {}
    holders = _num(m.get("holders"))
    return {"addr": addr, "net": int(net), "tid": tid,
            "ticker": m.get("symbol"),          # deployer-written: display and social only, never a label
            "mcap_usd": None if mcap is not None and mcap >= MCAP_UNKNOWN * 0.999 else mcap,
            "liquidity_usd": _num(m.get("liq")),
            "volume_h24": _num(m.get("vol24")), "price_usd": _num(m.get("price")),
            "holder_count": int(holders) if holders else None,
            # units as FOMO returns them (Codex documents decimals: 0.05 = +5%)
            "change": {"5m": _num(change.get(300)), "1h": _num(change.get(3600)),
                       "4h": _num(change.get(14400)), "24h": _num(change.get(86400))},
            "age_minutes": age_minutes(m.get("created"))}


def shortlist(fomo, ids: list[str]) -> list[dict]:
    """Pass one over everything FOMO knows: one call per twenty tokens, never one per token."""
    out = []
    for tid, m in fomo.tokens(ids).items():
        try:
            t = normalise(tid, m)
        except (ValueError, TypeError, AttributeError) as exc:
            log.warning("shortlist: skipping %s: %s", tid, exc)
            continue
        if t["net"] in GT_NET:
            out.append(t)

    def turnover(t):  # orders the work only; unknown ranks last, with no stand-in number
        v, c = t["volume_h24"], t["mcap_usd"]
        known = v is not None and bool(c)
        return (known, v / c if known else 0.0)
    out.sort(key=turnover, reverse=True)
    return out


# ---- pass two: DexScreener -------------------------------------------------------------------------
def trade_counts(t: dict) -> dict:
    """Buys, sells and volume per window from the deepest DexScreener pair where this token is the base.
    One call per token, only after free_kill passed it. "trade_data" says why counts are null:
    ok | no pair | unavailable | rate_limited | chain not on DexScreener. A failed request is never zero trades."""
    _require(t, "stage2")
    keys = ("buys_h1", "sells_h1", "buys_h6", "sells_h6", "trades_h24", "volume_h1", "volume_h6")
    empty = dict.fromkeys(keys)
    chain = DEX_CHAIN.get(GT_NET.get(t.get("net"), ""), None)
    if chain is None:
        return {**empty, "trade_data": "chain not on DexScreener"}
    try:
        body = _get_json(f"{DEX}/{chain}/{t['addr']}")
    except FetchFailed as exc:
        return {**empty, "trade_data": "rate_limited" if exc.status == 429 else "unavailable"}
    if isinstance(body, list):
        pairs = body
    elif isinstance(body, dict) and "pairs" in body:
        pairs = body["pairs"] or []
    else:
        return {**empty, "trade_data": "unavailable"}
    addr = t["addr"]
    same = (lambda a: a.lower() == addr.lower()) if addr.startswith("0x") else (lambda a: a == addr)
    mine = [p for p in pairs if isinstance(p, dict) and p.get("chainId") in (chain, None)
            and same(str((p.get("baseToken") or {}).get("address") or ""))]
    if not mine:
        return {**empty, "trade_data": "no pair"}
    best = max(mine, key=lambda p: (_num((p.get("liquidity") or {}).get("usd")) is not None,
                                    _num((p.get("liquidity") or {}).get("usd")) or 0.0))
    x, vol = best.get("txns") or {}, best.get("volume") or {}

    def n(window, side):
        v = (x.get(window) or {}).get(side)
        return int(v) if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    h24b, h24s = n("h24", "buys"), n("h24", "sells")
    return {"buys_h1": n("h1", "buys"), "sells_h1": n("h1", "sells"),
            "buys_h6": n("h6", "buys"), "sells_h6": n("h6", "sells"),
            "trades_h24": h24b + h24s if h24b is not None and h24s is not None else None,
            "volume_h1": _num(vol.get("h1")), "volume_h6": _num(vol.get("h6")), "trade_data": "ok"}


# ---- pass three: dossier ---------------------------------------------------------------------------
def dossier(t: dict) -> dict:
    """One GeckoTerminal slot. Solana adds three free RPC calls: authorities, extensions and holders come
    from the chain itself. Fills what is really there, null where it is not.
    404 = not indexed (real nulls, normal on Robinhood). Any other failure raises DossierFailed.
    Refuses (KilledToken) a token without both the stage-2 and stage-3 pass."""
    _require(t, "stage2", "stage3")
    net = GT_NET[t["net"]]
    try:
        body = _get_json(f"{GT}/networks/{net}/tokens/{t['addr']}/info", None, GT_LIMIT)
    except FetchFailed as exc:
        raise DossierFailed(f"GeckoTerminal token info: {exc}") from None
    if body is not None and not (isinstance(body, dict) and isinstance(body.get("data"), dict)):
        raise DossierFailed("GeckoTerminal token info: unexpected body")
    data = (body or {}).get("data") or {}
    a = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    holders = a.get("holders") if isinstance(a.get("holders"), dict) else {}
    dist = holders.get("distribution_percentage") if isinstance(holders.get("distribution_percentage"), dict) else {}
    gt_holders = int(_num(holders.get("count"))) if _num(holders.get("count")) else None

    d = {**t, "chain": net, "chain_set": CHAIN_SET.get(t["net"]),
         "gt_info_found": bool(a),
         "holder_count": gt_holders if gt_holders is not None else t.get("holder_count"),
         "holder_count_source": "geckoterminal" if gt_holders is not None else
                                ("fomo" if t.get("holder_count") is not None else None),
         "top_10_percent": _num(dist.get("top_10")),                     # percent, as GeckoTerminal sends it
         "developer_holding_percentage": _num(a.get("developer_holding_percentage")),
         "gt_score_details": a.get("gt_score_details") if isinstance(a.get("gt_score_details"), dict) else None,
         "is_honeypot": _honeypot(a.get("is_honeypot")),                 # True / False / "unknown" / None
         # Authorities exist only on Solana. On EVM chains they are not facts at all, so they stay null
         # and no EVM question reads them.
         "mint_authority_revoked": _revoked(a.get("mint_authority")) if net == "solana" else None,
         "freeze_authority_revoked": _revoked(a.get("freeze_authority")) if net == "solana" else None,
         "authority_source": "geckoterminal" if net == "solana" and a else None,
         "description": _clean_text(a.get("description"), 280),         # deployer-written: social only
         "x_handle": clean_handle(a.get("twitter_handle")),
         "top_wallet_percent": None, "top_program_account_percent": None, "sell_block_possible": None}

    if net == "solana":
        facts = sol_chain_facts(t["addr"])                  # the chain outranks GeckoTerminal
        for key in ("mint_authority_revoked", "freeze_authority_revoked"):
            if facts.get(key) is not None:
                d[key], d["authority_source"] = facts[key], "solana rpc"
        d.update({k: facts.get(k) for k in ("top_wallet_percent", "top_wallet_at_most_percent",
                                           "top_program_account_percent", "sell_block_possible",
                                           "risky_extensions", "decimals")})
    return d


def _honeypot(v):
    """GeckoTerminal documents a boolean or 'unknown'. Only a real True/False is a verdict."""
    if isinstance(v, bool) or v is None:
        return v
    return {"true": True, "yes": True, "false": False, "no": False}.get(str(v).strip().lower(), "unknown")


def _revoked(v):
    """GeckoTerminal's documented 'no' means no authority. Any other non-empty value = an authority exists."""
    if v is None or str(v).strip() == "":
        return None
    return str(v).strip().lower() in ("no", "none", "null", "revoked", "false")


def _clean_text(v, limit):
    if not isinstance(v, str):
        return None
    s = " ".join("".join(ch for ch in v if ch.isprintable()).split())
    return s[:limit] or None


def clean_handle(h):
    """'LuffyX100X/status/2102659581109272876' -> 'LuffyX100X'; URL forms parsed; X's ASCII pattern only."""
    if not isinstance(h, str) or not h.strip():
        return None
    h = h.strip()
    for prefix in ("https://", "http://", "www.", "x.com/", "twitter.com/", "mobile.twitter.com/"):
        if h.lower().startswith(prefix):
            h = h[len(prefix):]
    h = h.lstrip("@").split("?")[0].split("/")[0]
    return h if X_HANDLE.match(h) else None


# ---- Solana: facts from the chain itself -----------------------------------------------------------
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
RISKY_EXTENSIONS = {"transferFeeConfig", "permanentDelegate", "transferHook", "nonTransferable",
                    "defaultAccountState", "pausableConfig"}
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(s: str) -> bytes | None:
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw


def on_curve(address: str) -> bool | None:
    """True if the address is an ed25519 public key (a wallet someone holds a key for); False if it is
    off the curve, i.e. a program-derived address (pool vaults, bonding curves, multisigs). Same test as
    Solana's is_on_curve. None if the string is not a 32-byte base58 address."""
    raw = b58decode(address)
    if raw is None or len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    y %= _P
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1        # Euler: x^2 must be a square mod p


def _rpc(method, params):
    """The result, or None when every endpoint failed. A refusal is never read as an empty answer."""
    why = []
    for url in _rpc_urls():
        try:
            r = HTTP.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=10)
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            why.append(f"{_host(url)}: {type(exc).__name__}")
            continue
        if isinstance(body, dict) and "result" in body:
            _rpc_last_good[0] = url
            return body["result"]
        err = body.get("error") if isinstance(body, dict) else body
        msg = err.get("message") if isinstance(err, dict) else err
        why.append(f"{_host(url)}: {str(msg)[:80] or getattr(r, 'status_code', '?')}")
    log.warning("solana rpc %s failed everywhere: %s", method, "; ".join(why))
    RPC_FAILURES.append(f"{method}: " + "; ".join(why))
    del RPC_FAILURES[:-20]
    return None


def sol_chain_facts(mint: str) -> dict:
    """Three RPC calls, all free:
      getAccountInfo(mint)         -> authorities (a present null = revoked; an absent field = unknown),
                                      supply, Token-2022 extensions
      getTokenLargestAccounts      -> the 20 largest token accounts
      getMultipleAccounts          -> who owns each of them
    top_wallet_percent is the largest share held by a key-holding wallet (on-curve owner), in PERCENT.
    Pool vaults, bonding curves and other program accounts are off-curve and reported separately as
    top_program_account_percent: the largest account is nearly always the pool, which is not a holder."""
    out = {"mint_authority_revoked": None, "freeze_authority_revoked": None, "risky_extensions": None,
           "sell_block_possible": None, "top_wallet_percent": None, "top_wallet_at_most_percent": None,
           "top_program_account_percent": None, "decimals": None}
    info = ((_rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}).get("value")) or {}
    program = info.get("owner")
    parsed = ((info.get("data") or {}).get("parsed") or {}) if isinstance(info.get("data"), dict) else {}
    minfo = parsed.get("info") if parsed.get("type") == "mint" and isinstance(parsed.get("info"), dict) else None
    supply = None
    if minfo is not None and program in (SPL_TOKEN, TOKEN_2022):
        if "mintAuthority" in minfo:
            out["mint_authority_revoked"] = minfo["mintAuthority"] is None
        if "freezeAuthority" in minfo:
            out["freeze_authority_revoked"] = minfo["freezeAuthority"] is None
        exts = minfo.get("extensions")
        if isinstance(exts, list):
            names = {e.get("extension") for e in exts if isinstance(e, dict)}
            out["risky_extensions"] = sorted(n for n in names if n in RISKY_EXTENSIONS)
        elif program == SPL_TOKEN:
            out["risky_extensions"] = []                       # the classic program has no extensions
        freeze, risky = out["freeze_authority_revoked"], out["risky_extensions"]
        out["sell_block_possible"] = (True if freeze is False or risky else
                                      False if freeze is True and risky == [] else None)
        if isinstance(minfo.get("decimals"), int) and not isinstance(minfo.get("decimals"), bool):
            out["decimals"] = minfo["decimals"]
        try:
            supply = int(minfo.get("supply"))
        except (TypeError, ValueError):
            supply = None
    if not supply:
        return out

    top = ((_rpc("getTokenLargestAccounts", [mint]) or {}).get("value")) or []
    accounts = [(a.get("address"), int(a["amount"])) for a in top
                if isinstance(a, dict) and isinstance(a.get("address"), str) and str(a.get("amount", "")).isdigit()]
    if not accounts:
        return out
    owners = ((_rpc("getMultipleAccounts", [[a for a, _ in accounts], {"encoding": "jsonParsed"}]) or {})
              .get("value")) or []
    wallets, programs, unknown = {}, {}, 0
    for (addr, amount), acct in zip(accounts, owners + [None] * (len(accounts) - len(owners))):
        owner = ((((acct or {}).get("data") or {}).get("parsed") or {}).get("info") or {}).get("owner") \
            if isinstance((acct or {}).get("data"), dict) else None
        kind = on_curve(owner) if isinstance(owner, str) else None
        if kind is True:
            wallets[owner] = wallets.get(owner, 0) + amount
        elif kind is False:
            programs[owner] = programs.get(owner, 0) + amount
        else:
            unknown = max(unknown, amount)                 # could not tell who owns it
    if programs:
        out["top_program_account_percent"] = round(100.0 * max(programs.values()) / supply, 3)
    biggest_wallet = max(wallets.values()) if wallets else None
    if biggest_wallet is not None and biggest_wallet >= unknown:
        out["top_wallet_percent"] = round(100.0 * biggest_wallet / supply, 3)
    elif not wallets and not unknown and len(accounts) >= 20:
        # all 20 largest are program accounts: the biggest wallet holds at most the 20th's amount
        out["top_wallet_at_most_percent"] = round(100.0 * accounts[-1][1] / supply, 3)
    return out


# ---- the state each question set sees --------------------------------------------------------------
def market_state(d: dict, intended_ticket_usd) -> dict:
    """What the market set reads. No ticker: deployer-written text never reaches a market or chain question."""
    return {"age_minutes": round(d["age_minutes"], 1) if d.get("age_minutes") is not None else None,
            "holder_count": d.get("holder_count"), "change": d.get("change"),
            "buys_h1": d.get("buys_h1"), "sells_h1": d.get("sells_h1"),
            "buys_h6": d.get("buys_h6"), "sells_h6": d.get("sells_h6"),
            "volume_h1": d.get("volume_h1"), "volume_h6": d.get("volume_h6"), "volume_h24": d.get("volume_h24"),
            "liquidity_usd": d.get("liquidity_usd"), "mcap_usd": d.get("mcap_usd"),
            "intended_ticket_usd": intended_ticket_usd}


CHAIN_FIELDS = {
    "solana": ("mint_authority_revoked", "freeze_authority_revoked", "top_10_percent", "top_wallet_percent",
               "holder_count", "developer_holding_percentage"),
    "bsc": ("is_honeypot", "gt_score_details", "buys_h1", "sells_h1", "buys_h6", "sells_h6", "trades_h24",
            "top_10_percent", "holder_count", "liquidity_usd"),
    "robinhood": ("holder_count", "top_10_percent", "top_wallet_percent", "is_honeypot", "buys_h1", "sells_h1",
                  "buys_h6", "sells_h6", "trades_h24", "developer_holding_percentage", "age_minutes",
                  "volume_h24", "liquidity_usd", "price_usd"),
}


def chain_state(d: dict, chain_set: str) -> dict:
    """Exactly the fields that chain's set reads. Every key present; null stays null."""
    return {k: d.get(k) for k in CHAIN_FIELDS[chain_set]}


def social_state(d: dict) -> dict:
    """What SOCIAL hands the judge. x_account is filled by SOCIAL's X plugin, never here.
    The ticker, narrative and the account's posts are written by whoever launched the token: a prompt-
    injection channel. Every hard gate stays in code, so nothing in them can move a decision past one.
    x_account_status says why x_account is null; handle_published_by_token is what account_is_the_project
    compares the account against."""
    if d.get("x_account") is not None:
        status = "found"
    elif d.get("x_account_status"):
        status = str(d["x_account_status"])[:40]
    else:
        status = "no handle listed" if d.get("x_handle") is None else "not looked up"
    return {"x_account": d.get("x_account"), "x_account_status": status,
            "handle_published_by_token": d.get("x_handle"),
            "token": {"ticker": _clean_text(d.get("ticker"), 20),
                      "narrative": _clean_text(d.get("description"), 280),
                      "narrative_source": "deployer-written, unverified"}}
