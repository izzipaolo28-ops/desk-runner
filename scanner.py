#!/usr/bin/env python3
"""Read-only pump.fun token monitor. Never creates a wallet, signs, or submits trades."""
from __future__ import annotations

import base64
import csv
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from risk_score import score_token, format_token_report, trade_window_status

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pumpfun_scanner")

PUMP_API_URL = os.getenv(
    "PUMP_API_URL",
    "https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&sort=created_timestamp&order=DESC&includeNsfw=false",
)
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
CSV_PATH = Path(os.getenv("TOKENS_CSV", "tokens.csv"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TOP10_MAX_PCT = float(os.getenv("TOP10_MAX_PCT", "25"))
DEV_MAX_PCT = float(os.getenv("DEV_MAX_PCT", "5"))
MAX_BLOCK0_BUYERS = int(os.getenv("MAX_BLOCK0_BUYERS", "3"))
CURVE_MIN_PCT = float(os.getenv("CURVE_MIN_PCT", "15"))
CURVE_MAX_PCT = float(os.getenv("CURVE_MAX_PCT", "80"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12"))

CSV_FIELDS = [
    "checked_at_utc", "mint", "name", "symbol", "market_cap_usd",
    "curve_progress_pct", "top10_pct", "dev_pct", "block0_buyers",
    "score_0_to_10", "verdict", "score_unknowns", "passed", "reason", "token_url",
]


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str


def evaluate_filters(
    *,
    top10_pct: float | None,
    dev_pct: float | None,
    block0_buyers: int | None,
    curve_progress_pct: float | None,
) -> FilterResult:
    """Pure filter function; unknown measurements fail closed rather than guessing."""
    values = {
        "top10_pct": top10_pct,
        "dev_pct": dev_pct,
        "block0_buyers": block0_buyers,
        "curve_progress_pct": curve_progress_pct,
    }
    missing = [k for k, v in values.items() if v is None]
    if missing:
        return FilterResult(False, "missing metrics: " + ", ".join(missing))
    if not 0 <= top10_pct <= 100:
        return FilterResult(False, "invalid top10_pct")
    if not 0 <= dev_pct <= 100:
        return FilterResult(False, "invalid dev_pct")
    if block0_buyers < 0:
        return FilterResult(False, "invalid block0_buyers")
    if not 0 <= curve_progress_pct <= 100:
        return FilterResult(False, "invalid curve_progress_pct")
    failures = []
    if top10_pct >= TOP10_MAX_PCT:
        failures.append(f"top10 {top10_pct:.2f}% >= {TOP10_MAX_PCT:.2f}%")
    if dev_pct >= DEV_MAX_PCT:
        failures.append(f"dev {dev_pct:.2f}% >= {DEV_MAX_PCT:.2f}%")
    if block0_buyers > MAX_BLOCK0_BUYERS:
        failures.append(f"block-0 buyers {block0_buyers} > {MAX_BLOCK0_BUYERS}")
    if not CURVE_MIN_PCT <= curve_progress_pct <= CURVE_MAX_PCT:
        failures.append(
            f"curve {curve_progress_pct:.2f}% outside {CURVE_MIN_PCT:.2f}-{CURVE_MAX_PCT:.2f}%"
        )
    return FilterResult(not failures, "passed" if not failures else "; ".join(failures))


def _request_json(url: str, *, method: str = "GET", payload: dict | None = None, headers: dict | None = None) -> Any:
    response = requests.request(method, url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def fetch_new_tokens() -> list[dict]:
    """Fetch newest public pump.fun listings. Endpoint shape can change; errors are logged."""
    data = _request_json(PUMP_API_URL)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("coins", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    raise ValueError("pump.fun API response did not contain a token list")


def helius_rpc(method: str, params: list) -> Any:
    if not HELIUS_RPC_URL:
        raise RuntimeError("HELIUS_RPC_URL is missing; set it in .env")
    result = _request_json(
        HELIUS_RPC_URL,
        method="POST",
        payload={"jsonrpc": "2.0", "id": "pumpfun-readonly", "method": method, "params": params},
        headers={"Content-Type": "application/json"},
    )
    if result.get("error"):
        raise RuntimeError(f"Helius RPC error: {result['error']}")
    return result.get("result")


def get_token_supply(mint: str) -> float:
    result = helius_rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
    return float(result["value"]["uiAmount"] or 0)


def get_top_holder_distribution(mint: str, creator: str | None) -> tuple[float, float]:
    """Aggregate largest token accounts by owner, avoiding counting split accounts as separate holders.

    Solana token account data layout: mint[0:32], owner[32:64], amount[64:72].
    Helius returns base64 account data for getMultipleAccounts.
    This samples the 20 largest token accounts returned by RPC; it is an estimate of top-10
    wallet concentration, not a full exhaustive holder index.
    """
    supply = get_token_supply(mint)
    if supply <= 0:
        raise ValueError("token supply unavailable")
    largest = helius_rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    token_accounts = (largest or {}).get("value", [])
    addresses = [a.get("address") for a in token_accounts if a.get("address")]
    if not addresses:
        raise ValueError("no largest token accounts returned")
    multi = helius_rpc("getMultipleAccounts", [addresses, {"encoding": "base64", "commitment": "confirmed"}])
    accounts = (multi or {}).get("value", [])
    owner_amounts: dict[str, float] = {}
    for entry, account in zip(token_accounts, accounts):
        if not account or not account.get("data"):
            continue
        raw = base64.b64decode(account["data"][0])
        if len(raw) < 72:
            continue
        owner = __import__("base64").b64encode(raw[32:64]).decode("ascii")
        # Public keys are base58, not base64; use solders when installed, otherwise fail closed.
        try:
            from solders.pubkey import Pubkey
            owner = str(Pubkey.from_bytes(raw[32:64]))
        except Exception:
            raise RuntimeError("Install solders to decode Solana account owners")
        amount_raw = int.from_bytes(raw[64:72], "little")
        decimals = int(account.get("space", 0))  # not token decimals; use RPC uiAmount below when possible
        token_amount = float(entry.get("uiAmount") or 0)
        owner_amounts[owner] = owner_amounts.get(owner, 0.0) + token_amount
    ranked = sorted(owner_amounts.values(), reverse=True)
    top10_pct = min(100.0, sum(ranked[:10]) / supply * 100.0)
    dev_pct = min(100.0, owner_amounts.get(creator or "", 0.0) / supply * 100.0)
    return top10_pct, dev_pct


def get_block0_buyers(mint: str) -> int:
    """Estimate unique buyers in the mint's earliest transaction slot using Helius RPC.

    This intentionally fails closed when transaction data is unavailable. RPC pagination
    and transaction decoding are provider-dependent; this method counts unique non-creator
    signers among signatures in the earliest observed slot, so treat it as an estimate.
    """
    signatures = helius_rpc("getSignaturesForAddress", [mint, {"limit": 1000, "commitment": "confirmed"}]) or []
    if not signatures:
        raise ValueError("no mint signatures available")
    earliest_slot = min(int(s.get("slot", 0)) for s in signatures)
    buyers: set[str] = set()
    for sig in signatures:
        if int(sig.get("slot", -1)) != earliest_slot:
            continue
        tx = helius_rpc("getTransaction", [sig["signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"
        }])
        if not tx:
            continue
        message = (tx.get("transaction") or {}).get("message") or {}
        account_keys = message.get("accountKeys") or []
        for key in account_keys:
            if isinstance(key, dict) and key.get("signer"):
                buyers.add(key.get("pubkey", ""))
            elif isinstance(key, str):
                # Without parsed signer metadata we cannot reliably distinguish signers.
                continue
    buyers.discard("")
    if not buyers:
        raise ValueError("could not determine block-0 buyers from available transaction data")
    return len(buyers)


def token_fields(token: dict) -> tuple[str, str, str, float, float]:
    mint = str(token.get("mint") or token.get("address") or token.get("tokenAddress") or "")
    if not mint:
        raise ValueError("token has no mint/address")
    name = str(token.get("name") or "")
    symbol = str(token.get("symbol") or "")
    market_cap = token.get("usd_market_cap", token.get("market_cap", token.get("marketCap", 0)))
    try:
        market_cap = float(market_cap or 0)
    except (TypeError, ValueError):
        market_cap = 0.0
    curve = token.get("bonding_curve_progress", token.get("curve_progress", token.get("curveProgress")))
    if curve is None:
        curve_pct = None
    else:
        curve_pct = float(curve)
        # Some APIs express curve progress as 0..1 rather than 0..100.
        if 0 <= curve_pct <= 1:
            curve_pct *= 100
    creator = token.get("creator") or token.get("user") or token.get("creator_address")
    return mint, name, symbol, market_cap, curve_pct, creator


def token_url(mint: str) -> str:
    return f"https://pump.fun/coin/{mint}"


def append_csv(row: dict) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = CSV_PATH.exists() and CSV_PATH.stat().st_size > 0
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})


def telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured; passing token will only be logged")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    _request_json(url, method="POST", payload={
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True
    })


def process_token(token: dict) -> dict:
    mint, name, symbol, market_cap, curve_pct, creator = token_fields(token)
    metrics = {"top10_pct": None, "dev_pct": None, "block0_buyers": None}
    errors = []
    try:
        metrics["top10_pct"], metrics["dev_pct"] = get_top_holder_distribution(mint, creator)
    except Exception as exc:
        errors.append(f"holder data unavailable: {exc}")
    try:
        metrics["block0_buyers"] = get_block0_buyers(mint)
    except Exception as exc:
        errors.append(f"block-0 data unavailable: {exc}")
    result = evaluate_filters(
        top10_pct=metrics["top10_pct"],
        dev_pct=metrics["dev_pct"],
        block0_buyers=metrics["block0_buyers"],
        curve_progress_pct=curve_pct,
        token_age_hours=None,
        new_ath_within_6h=None,
        dev_sold_within_10m=None,
    )
    reason = result.reason
    if errors:
        reason = "; ".join(errors + [reason])
        passed = False
    else:
        passed = result.passed
    score_result = score_token(top10_pct=metrics["top10_pct"], dev_pct=metrics["dev_pct"], block0_buyers=metrics["block0_buyers"], curve_progress_pct=curve_pct)
    url = token_url(mint)
    row = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "mint": mint, "name": name, "symbol": symbol, "market_cap_usd": market_cap,
        "curve_progress_pct": "" if curve_pct is None else round(curve_pct, 4),
        "top10_pct": "" if metrics["top10_pct"] is None else round(metrics["top10_pct"], 4),
        "dev_pct": "" if metrics["dev_pct"] is None else round(metrics["dev_pct"], 4),
        "block0_buyers": "" if metrics["block0_buyers"] is None else metrics["block0_buyers"],
        "score_0_to_10": score_result.score, "verdict": score_result.verdict,
        "score_unknowns": " | ".join(score_result.unknowns),
        "passed": passed, "reason": reason, "token_url": url,
    }
    append_csv(row)
    if passed:
        telegram_send(
            f"🟢 Read-only pump.fun filter passed\n"
            f"{name} ({symbol})\nCA: {mint}\n"
            f"MCap: ${market_cap:,.0f}\nCurve: {curve_pct:.2f}%\n"
            f"Top 10: {metrics['top10_pct']:.2f}%\nDev: {metrics['dev_pct']:.2f}%\n"
            f"Block-0 buyers: {metrics['block0_buyers']}\n"
            f"Research score: {score_result.score}/10 — {score_result.verdict}\n"
            f"Unknown signals: {'; '.join(score_result.unknowns) or 'none'}\n{url}\n"
            f"Research only — not a buy signal; no trade was placed."
        )
    return row


def main() -> None:
    seen: set[str] = set()
    log.info("Starting read-only pump.fun scanner; polling every %ss", POLL_SECONDS)
    while True:
        started = time.monotonic()
        try:
            for token in fetch_new_tokens():
                try:
                    mint, *_ = token_fields(token)
                    if mint in seen:
                        continue
                    seen.add(mint)
                    row = process_token(token)
                    log.info("%s %s: %s", "PASS" if row["passed"] else "FAIL", mint, row["reason"])
                except Exception as exc:
                    log.exception("Could not process token: %s", exc)
                    # If mint is known, record the failure; malformed API rows are logged to app logs.
                    mint = str(token.get("mint") or token.get("address") or "")
                    if mint:
                        append_csv({
                            "checked_at_utc": datetime.now(timezone.utc).isoformat(), "mint": mint,
                            "name": token.get("name", ""), "symbol": token.get("symbol", ""),
                            "passed": False, "reason": f"processing error: {exc}", "token_url": token_url(mint),
                        })
        except Exception as exc:
            log.warning("Token poll failed: %s", exc)
        elapsed = time.monotonic() - started
        time.sleep(max(1, POLL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
