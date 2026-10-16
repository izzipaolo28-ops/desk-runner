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


# ---- SOL transfers: funding the bot from your wallet, withdrawing back to it -----------------------------
SYSTEM_PROGRAM = "11111111111111111111111111111111"
LAMPORTS_PER_SOL = 1_000_000_000


def compact_u16(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _key32(address: str, what: str) -> bytes:
    raw = b58decode(address)
    if len(raw) != 32:
        raise ValueError(f"{what} is not a Solana address")
    return raw


def transfer_message(from_addr: str, to_addr: str, lamports: int, blockhash: str) -> bytes:
    """A legacy Solana message with one System Program transfer: `from` pays the fee and signs, `to` receives.
    Layout: header (1 signature, 0 read-only signed, 1 read-only unsigned: the program), the three account keys,
    the recent blockhash, then one instruction (program 2, accounts [0, 1], data = u32 2 "Transfer" + u64 lamports)."""
    if not isinstance(lamports, int) or isinstance(lamports, bool) or not 0 < lamports < 2 ** 64:
        raise ValueError("the amount must be a positive whole number of lamports")
    frm, to = _key32(from_addr, "the sending wallet"), _key32(to_addr, "the receiving wallet")
    if frm == to:
        raise ValueError("a wallet can't send to itself")
    data = (2).to_bytes(4, "little") + lamports.to_bytes(8, "little")
    return (bytes([1, 0, 1]) + compact_u16(3) + frm + to + _key32(SYSTEM_PROGRAM, "the system program")
            + _key32(blockhash, "the blockhash") + compact_u16(1)
            + bytes([2]) + compact_u16(2) + bytes([0, 1]) + compact_u16(len(data)) + data)


def signed_transfer(keypair: Keypair, to_addr: str, lamports: int, blockhash: str) -> tuple[str, str]:
    """(base64 transaction, its signature in base58) for the bot wallet sending `lamports` to `to_addr`."""
    msg = transfer_message(keypair.address, to_addr, lamports, blockhash)
    sig = keypair.sign(msg)
    return base64.b64encode(compact_u16(1) + sig + msg).decode(), b58encode(sig)


def verify_signed_message(address: str, message: bytes, signature: bytes) -> bool:
    """Did the wallet `address` sign `message` (Phantom's signMessage: an Ed25519 signature over the bytes)?"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(_key32(address, "the wallet")).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


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
            err = body.get("error") if isinstance(body, dict) else None
            why = str(err.get("message") if isinstance(err, dict) else err or "")[:120]   # the network's reason, never the URL's key
            last = f"error from {url.split('/')[2].split('?')[0]}" + (f": {why}" if why else "")
        raise RuntimeError(f"Solana RPC {method}: {last}")

    def sol_lamports(self, owner: str) -> int:
        v = (self.call("getBalance", [owner]) or {}).get("value")
        if not isinstance(v, int):
            raise RuntimeError("getBalance gave no value")
        return v

    def latest_blockhash(self) -> str:
        v = (self.call("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("value") or {}
        bh = v.get("blockhash")
        if not isinstance(bh, str) or len(b58decode(bh)) != 32:
            raise RuntimeError("getLatestBlockhash gave no blockhash")
        return bh

    def send_transaction(self, tx_b64: str) -> str:
        sig = self.call("sendTransaction", [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}])
        if not isinstance(sig, str):
            raise RuntimeError("sendTransaction gave no signature")
        return sig

    def signature_status(self, sig: str):
        """"confirmed" / "finalized" / "failed: ..." / None (not seen yet)."""
        st = ((self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}]) or {}).get("value") or [None])[0]
        if not isinstance(st, dict):
            return None
        if st.get("err"):
            return f"failed: {str(st['err'])[:80]}"
        return st.get("confirmationStatus") or "processed"

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
