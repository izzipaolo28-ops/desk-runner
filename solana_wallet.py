# solana_wallet.py
"""The bot's own Solana wallet: load the key, sign a swap transaction, read balances.

The key comes only from the BOT_WALLET_KEY environment variable (Railway keeps it encrypted). It is never
logged, never sent anywhere, never shown on the dashboard. Accepted formats: the base58 string Phantom shows
under "Show Private Key", or the JSON array of 64 numbers the Solana CLI writes.
Use a NEW wallet made only for the bot, holding only what you are willing to lose.
"""
import base64
import json
import logging

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

log = logging.getLogger(__name__)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = B58.find(ch)
        if i < 0:
            raise ValueError("not base58")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw


class WalletKeyError(ValueError):
    """The key is malformed. The message never contains the key."""


class Keypair:
    def __init__(self, secret64: bytes):
        if len(secret64) != 64:
            raise WalletKeyError("the wallet key must be 64 bytes")
        self._key = Ed25519PrivateKey.from_private_bytes(secret64[:32])
        self.public = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if self.public != secret64[32:]:
            raise WalletKeyError("the wallet key's two halves don't match (copied wrong?)")
        self.address = b58encode(self.public)

    @classmethod
    def from_env(cls, value: str) -> "Keypair":
        text = (value or "").strip()
        if not text:
            raise WalletKeyError("BOT_WALLET_KEY is empty")
        try:
            raw = bytes(json.loads(text)) if text.startswith("[") else b58decode(text)
        except (ValueError, TypeError):
            raise WalletKeyError("BOT_WALLET_KEY is neither base58 nor a JSON array of numbers") from None
        return cls(raw)

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)

    def __repr__(self):                          # never the key
        return f"Keypair({self.address})"


def _read_compact(buf: bytes, i: int) -> tuple[int, int]:
    """Solana's compact-u16: 7 bits per byte, low first, high bit = more."""
    value = shift = 0
    for _ in range(3):
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
    raise ValueError("bad compact-u16")


def sign_transaction(tx_b64: str, keypair: Keypair) -> str:
    """Add our signature to a serialized transaction (legacy or v0) in the slot that belongs to our key.
    Other signature slots (e.g. a market maker's, added later by Jupiter) are left exactly as they are."""
    raw = base64.b64decode(tx_b64)
    n_sigs, off = _read_compact(raw, 0)
    sigs_end = off + 64 * n_sigs
    message = raw[sigs_end:]
    i = 0
    if message[0] & 0x80:
        version = message[0] & 0x7F
        if version != 0:
            raise ValueError(f"transaction version {version} is not supported; ask for version 0")
        i = 1
    n_required = message[i]
    i += 3                                       # header: required sigs, readonly signed, readonly unsigned
    n_keys, i = _read_compact(message, i)
    keys = [message[i + 32 * k: i + 32 * k + 32] for k in range(n_keys)]
    if n_required != n_sigs:
        raise ValueError("signature count does not match the message header")
    try:
        slot = keys[:n_required].index(keypair.public)
    except ValueError:
        raise ValueError("this transaction does not ask the bot's wallet to sign") from None
    sig = keypair.sign(message)
    signed = raw[:off + 64 * slot] + sig + raw[off + 64 * (slot + 1):]
    return base64.b64encode(signed).decode()


# ---- balances, from any Solana RPC (server side) ----------------------------------------------------
RPC_FALLBACKS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com",
                 "https://solana.drpc.org"]


class RPC:
    def __init__(self, urls=None, session=None):
        self.urls = [u for u in (urls or []) if u] + [u for u in RPC_FALLBACKS if u not in (urls or [])]
        self.http = session or requests.Session()

    def call(self, method, params):
        last = "no endpoint answered"
        for url in self.urls:
            try:
                r = self.http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
                body = r.json()
            except (requests.RequestException, ValueError) as exc:
                last = f"{type(exc).__name__} from {url.split('/')[2]}"
                continue
            if isinstance(body, dict) and "result" in body:
                return body["result"]
            last = f"error from {url.split('/')[2]}"
        raise RuntimeError(f"Solana RPC {method}: {last}")

    def sol_lamports(self, owner: str) -> int:
        v = (self.call("getBalance", [owner]) or {}).get("value")
        if not isinstance(v, int):
            raise RuntimeError("getBalance gave no value")
        return v

    def token_balance(self, owner: str, mint: str) -> tuple[int, int | None]:
        """(raw amount, decimals) summed over the owner's token accounts for this mint. (0, None) if none."""
        res = self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
        total, decimals = 0, None
        for acct in res.get("value") or []:
            info = ((((acct or {}).get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            amount = (info.get("tokenAmount") or {})
            if str(amount.get("amount", "")).isdigit():
                total += int(amount["amount"])
                decimals = amount.get("decimals") if isinstance(amount.get("decimals"), int) else decimals
        return total, decimals
