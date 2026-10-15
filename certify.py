# certify.py
"""The desk's "certified" safety gate.

A desk pick is only CERTIFIED when every check below passes on facts that were actually read. An unknown is a
fail, never a pass. Certified coins are the only ones the desk buys (DESK_CERTIFIED=on, the default), and they get
the desk's longer-hold plan. This lowers the odds of buying a rug or a dump; it does not make a coin safe.

certify(rep, limits) -> (ok, [why not, ...])
  rep     the sniper's report on the coin (snipe.Sniper.check)
  limits  a dict of numbers, see DEFAULTS and limits_from_env
"""
import os

CURVE_DEXES = {"pumpfun", "pump.fun", "moonshot", "launchlab", "boop"}   # still on a bonding curve: no LP to pull
DEFAULTS = {
    "score": 75.0,             # the sniper's score (0-100)
    "min_liq_usd": 50_000.0,   # pool liquidity
    "min_age_hours": 6.0,
    "min_holders": 500,
    "max_top_wallet_pct": 5.0,
    "max_top10_pct": 35.0,
    "max_dev_pct": 3.0,
    "max_insider_pct": 3.0,
    "max_cluster_pct": 2.0,    # share of the supply held by top holders one wallet funded
    "max_fresh_pct": 20.0,     # share of traced top holders that are brand-new wallets
    "min_lp_locked_pct": 50.0,
    "deep_liq_usd": 250_000.0, # a pool this deep and a day old may pass with no LP lock (many older pools have none)
    "deep_age_hours": 24.0,
}
_ENV = {
    "score": "DESK_CERT_SCORE", "min_liq_usd": "DESK_CERT_MIN_LIQ_USD", "min_age_hours": "DESK_CERT_MIN_AGE_HOURS",
    "min_holders": "DESK_CERT_MIN_HOLDERS", "max_top_wallet_pct": "DESK_CERT_MAX_TOP_WALLET_PCT",
    "max_top10_pct": "DESK_CERT_MAX_TOP10_PCT", "max_dev_pct": "DESK_CERT_MAX_DEV_PCT",
    "max_insider_pct": "DESK_CERT_MAX_INSIDER_PCT", "max_cluster_pct": "DESK_CERT_MAX_CLUSTER_PCT",
    "max_fresh_pct": "DESK_CERT_MAX_FRESH_PCT", "min_lp_locked_pct": "DESK_CERT_MIN_LP_PCT",
}


def limits_from_env(env=os.environ):
    """DEFAULTS, with any DESK_CERT_* variable that is set. A set variable that isn't a number stops the desk and
    names the variable (never silently replaced with the default)."""
    out = dict(DEFAULTS)
    for key, name in _ENV.items():
        raw = str(env.get(name, "")).strip()
        if not raw:
            continue
        try:
            v = float(raw)
        except ValueError:
            raise ValueError(f"{name} must be a number") from None
        if v != v or v < 0:
            raise ValueError(f"{name} must be a number, 0 or more")
        out[key] = v
    return out


def _n(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def certify(rep, limits=None):
    L = {**DEFAULTS, **(limits or {})}
    F, rug, mp = rep.get("facts") or {}, rep.get("rug") or {}, rep.get("map") or {}
    no = []

    def need(value, label):
        """A number we must have; None = couldn't be read = not certified."""
        v = _n(value)
        if v is None:
            no.append(f"{label} unknown")
        return v

    if rep.get("verdict") == "SKIP":
        no.append("the rug checks say SKIP")
    if rug.get("rugged") is True:
        no.append("RugCheck marks it rugged")
    elif rug.get("rugged") is None:
        no.append("RugCheck's full report unavailable")
    if F.get("mint_revoked") is not True:
        no.append("mint authority not confirmed revoked")
    if F.get("freeze_revoked") is not True:
        no.append("freeze authority not confirmed revoked")
    if F.get("risky_extensions"):
        no.append("Token-2022 extensions that can trap holders")

    score = need(rep.get("score"), "score")
    if score is not None and score < L["score"]:
        no.append(f"score {score:g} is under {L['score']:g}")
    liq = need(F.get("liq"), "liquidity")
    if liq is not None and liq < L["min_liq_usd"]:
        no.append(f"liquidity ${liq:,.0f} is under ${L['min_liq_usd']:,.0f}")
    age = need(F.get("age_min"), "age")
    age_h = age / 60 if age is not None else None
    if age_h is not None and age_h < L["min_age_hours"]:
        no.append(f"only {age_h:.1f}h old (needs {L['min_age_hours']:g}h)")
    holders = need(F.get("holders"), "holder count")
    if holders is not None and holders < L["min_holders"]:
        no.append(f"{holders:.0f} holders (needs {L['min_holders']:g})")
    for key, label, limit in (("top_wallet_pct", "biggest wallet", "max_top_wallet_pct"), ("top10_pct", "top 10 wallets", "max_top10_pct"),
                              ("dev_pct", "dev holding", "max_dev_pct")):
        v = need(F.get(key), label)
        if v is not None and v > L[limit]:
            no.append(f"{label} {v:.1f}% (max {L[limit]:g}%)")
    ins = need(rug.get("insider_pct"), "insider share")
    if ins is not None and ins > L["max_insider_pct"]:
        no.append(f"insiders hold {ins:.1f}% (max {L['max_insider_pct']:g}%)")

    lp = _n(rug.get("lp_locked"))
    deep = liq is not None and age_h is not None and liq >= L["deep_liq_usd"] and age_h >= L["deep_age_hours"]
    curve = str(F.get("dex") or "").lower() in CURVE_DEXES
    if lp is None:
        if not deep and not curve:
            no.append("liquidity lock unknown")
    elif lp < L["min_lp_locked_pct"] and not deep:
        no.append(f"only {lp:.0f}% of the pool is locked or burned")

    if mp.get("status") != "ok":
        no.append(f"bubble map not read ({mp.get('status') or 'no answer'})")
    else:
        cp = _n(mp.get("clustered_pct")) or 0.0
        if cp > L["max_cluster_pct"]:
            no.append(f"{cp:.1f}% of the supply sits in funded clusters (max {L['max_cluster_pct']:g}%)")
        tr, fr = _n(mp.get("traced")), _n(mp.get("fresh"))
        if tr and tr >= 5 and fr is not None and 100.0 * fr / tr > L["max_fresh_pct"]:
            no.append(f"{100.0 * fr / tr:.0f}% of the traced top holders are brand-new wallets (max {L['max_fresh_pct']:g}%)")
    return (not no), no
