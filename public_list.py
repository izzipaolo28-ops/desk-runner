# public_list.py
"""Pass one without FOMO. FOMO's terms forbid automated access and its list endpoint is not public, so the
desk reads the same facts from DexScreener's documented token endpoint instead: up to 30 tokens per call.

It looks exactly like the FOMO client to main.py (.tokens(ids) and .token()), and its rows have FOMO's field
names, so collect.normalise() reads them unchanged. A value DexScreener doesn't give stays null:
holders (GeckoTerminal fills it in the dossier), the 4h change (DexScreener has 6h, which is not 4h), and
market cap when DexScreener has none (FDV never stands in for it).
"""
import logging

import collect

log = logging.getLogger(__name__)
BATCH = 30


class PublicList:
    def __init__(self):
        self.calls = 0

    def token(self):
        """FOMO's bearer refresh. Nothing to refresh here."""

    def tokens(self, ids: list[str]) -> dict:
        by_net: dict[int, list[str]] = {}
        for tid in ids:
            addr, _, net = tid.rpartition(":")
            if addr and net.isdigit() and int(net) in collect.GT_NET:
                by_net.setdefault(int(net), []).append(addr)
        out = {}
        for net, addrs in by_net.items():
            chain = collect.DEX_CHAIN.get(collect.GT_NET[net])
            if chain is None:
                continue
            for i in range(0, len(addrs), BATCH):
                chunk = addrs[i:i + BATCH]
                try:
                    self.calls += 1
                    body = collect._get_json(f"{collect.DEX}/{chain}/{','.join(chunk)}")
                except collect.FetchFailed as exc:
                    log.warning("public list: %s", exc)
                    continue
                pairs = body if isinstance(body, list) else []
                for addr in chunk:
                    pair = _deepest(pairs, addr, chain)
                    if pair is not None:
                        out[f"{addr}:{net}"] = _row(pair)
        return out


def _same(a, b):
    return a.lower() == b.lower() if a.startswith("0x") else a == b


def _deepest(pairs, addr, chain):
    mine = [p for p in pairs if isinstance(p, dict) and p.get("chainId") in (chain, None)
            and _same(str((p.get("baseToken") or {}).get("address") or ""), addr)]
    if not mine:
        return None
    liq = lambda p: collect._num((p.get("liquidity") or {}).get("usd"))
    return max(mine, key=lambda p: (liq(p) is not None, liq(p) or 0.0))


def _pct_to_ratio(v):
    n = collect._num(v)
    return None if n is None else n / 100.0          # DexScreener sends percent; the desk uses ratios (0.05 = +5%)


def _row(p):
    change = p.get("priceChange") or {}
    return {"symbol": (p.get("baseToken") or {}).get("symbol"),
            "mcap": p.get("marketCap"),               # a reported market cap only, never fdv
            "liq": (p.get("liquidity") or {}).get("usd"),
            "vol24": (p.get("volume") or {}).get("h24"),
            "price": p.get("priceUsd"),
            "holders": None,
            "change": {300: _pct_to_ratio(change.get("m5")), 3600: _pct_to_ratio(change.get("h1")),
                       14400: None, 86400: _pct_to_ratio(change.get("h24"))},
            "created": p.get("pairCreatedAt")}
