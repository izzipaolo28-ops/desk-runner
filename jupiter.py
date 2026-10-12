# jupiter.py
"""Jupiter Swap API v2 (developers.jup.ag, "Order & Execute"):
    GET  https://api.jup.ag/swap/v2/order?inputMint&outputMint&amount&taker   -> quote (+ unsigned tx if taker)
    POST https://api.jup.ag/swap/v2/execute {signedTransaction, requestId}     -> Jupiter lands it, reports amounts
Header x-api-key (free key from the Jupiter developer portal). Without `taker` it is a quote only, which is
what paper mode uses: real prices, nothing sent. We ask for transaction version 0, which this bot can sign.
"""
import logging

import requests

log = logging.getLogger(__name__)
BASE = "https://api.jup.ag/swap/v2"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class JupiterError(RuntimeError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class Jupiter:
    def __init__(self, api_key=None, session=None, base=BASE):
        self.key, self.http, self.base = (api_key or "").strip() or None, session or requests.Session(), base

    def _headers(self):
        h = {"Accept": "application/json"}
        if self.key:
            h["x-api-key"] = self.key
        return h

    def _check(self, r, what):
        if r.status_code == 429:
            raise JupiterError(f"{what}: rate limited (429)", 429)
        if r.status_code in (401, 403):
            raise JupiterError(f"{what}: Jupiter refused the API key ({r.status_code})", r.status_code)
        if r.status_code != 200:
            raise JupiterError(f"{what}: HTTP {r.status_code}", r.status_code)
        try:
            return r.json()
        except ValueError:
            raise JupiterError(f"{what}: not JSON") from None

    def order(self, input_mint, output_mint, amount_raw: int, taker=None) -> dict:
        if not isinstance(amount_raw, int) or amount_raw <= 0:
            raise JupiterError("order: amount must be a positive whole number of base units")
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
                  "maxSupportedTransactionVersion": "0"}
        if taker:
            params["taker"] = taker
        body = self._check(self.http.get(f"{self.base}/order", params=params, headers=self._headers(), timeout=20), "order")
        if not isinstance(body, dict) or not str(body.get("outAmount", "")).isdigit():
            raise JupiterError(f"order: no outAmount ({str(body.get('errorMessage') or body.get('error') or '')[:120]})",
                               code=body.get("errorCode") if isinstance(body, dict) else None)
        if taker and not body.get("transaction"):
            raise JupiterError(f"order: quote but no transaction ({body.get('router')}: "
                               f"{body.get('errorCode')} {str(body.get('errorMessage') or '')[:100]})",
                               code=body.get("errorCode"))
        return body

    def execute(self, signed_tx_b64: str, request_id: str) -> dict:
        r = self.http.post(f"{self.base}/execute", json={"signedTransaction": signed_tx_b64, "requestId": request_id},
                           headers={**self._headers(), "Content-Type": "application/json"}, timeout=60)
        body = self._check(r, "execute")
        if body.get("status") != "Success":
            raise JupiterError(f"execute failed: code {body.get('code')} {str(body.get('error') or '')[:160]}",
                               code=body.get("code"))
        return body

    def sol_usd(self) -> float:
        """USD per SOL from a real 1 SOL -> USDC quote (USDC has 6 decimals)."""
        q = self.order(WSOL, USDC, 1_000_000_000)
        return int(q["outAmount"]) / 1e6
