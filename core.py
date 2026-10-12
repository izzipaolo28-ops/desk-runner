"""Desk logic with no FastAPI or SDK imports, so it can be tested anywhere."""
import collections
import hmac
import json
import threading
import time

MAX_STATE_BYTES = 100_000   # Jev allows ~32k tokens for state + the longest question; refuse early
MIN_SECRET_LEN = 32


class DeskError(Exception):
    def __init__(self, status: int, detail: str, headers: dict | None = None):
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers


class RequestCap:
    """At most ``per_minute`` accepted requests in any 60 s (protects the TypeSafe bill from a runaway bot).
    Non-blocking: over the cap, the request is refused with 429 + Retry-After instead of queued."""

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute, self._clock = max(1, int(per_minute)), clock
        self._times: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            now = self._clock()
            while self._times and now - self._times[0] >= 60.0:
                self._times.popleft()
            if len(self._times) >= self.per_minute:
                wait = max(1, int(60.0 - (now - self._times[0])) + 1)
                raise DeskError(429, f"desk limit of {self.per_minute} requests/min reached", {"Retry-After": str(wait)})
            self._times.append(now)


def cap_from_env(env) -> int:
    try:
        return max(1, int(env.get("DESK_MAX_REQUESTS_PER_MIN", 600)))
    except (TypeError, ValueError):
        return 600


def load_secret(env) -> str:
    secret = str(env.get("DESK_SECRET", "")).strip()
    if len(secret) < MIN_SECRET_LEN:
        raise RuntimeError(f"DESK_SECRET must be at least {MIN_SECRET_LEN} characters. Make one with: "
                           "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    return secret


def check_auth(header: str, secret: str) -> None:
    """Constant-time comparison, so the secret can't be guessed byte by byte from response timing."""
    if not hmac.compare_digest(str(header or "").encode(), f"Bearer {secret}".encode()):
        raise DeskError(401, "bad desk secret")


def check_size(state) -> None:
    size = len(json.dumps(state, separators=(",", ":")).encode())
    if size > MAX_STATE_BYTES:
        raise DeskError(413, f"state is {size} bytes; the desk accepts at most {MAX_STATE_BYTES}")


def build_questions(question_set: str, state: dict, sets: dict) -> dict:
    if question_set not in sets:
        raise DeskError(422, f"unknown question set; this desk serves: {sorted(sets)}")
    qs = sets[question_set]
    try:
        return qs(state) if callable(qs) else qs   # pick builds its options at call time
    except ValueError as exc:
        raise DeskError(422, f"bad state for {question_set}: {exc}") from None


def passthrough(raw: dict) -> dict:
    """Raw answers out: never flattened, never thresholded here."""
    if not isinstance(raw, dict) or "answers" not in raw:
        raise DeskError(502, "TypeSafe returned a response without answers")
    return {"model": raw.get("model"), "answers": raw.get("answers"), "usage": raw.get("usage")}


def map_error(exc: Exception, redact: str = "") -> DeskError:
    """TypeSafe/SDK failure -> what the bot should see. The TypeSafe key is scrubbed from any text."""
    status = getattr(exc, "status", None)
    text = str(exc)
    if redact:
        text = text.replace(redact, "[redacted]")
    if status in (429, 529):
        return DeskError(503, "TypeSafe is rate-limiting or overloaded; retry shortly", {"Retry-After": "2"})
    if status == 401:  # NOT 401 to the bot: its DESK_SECRET was fine, the desk's TypeSafe key is the problem
        return DeskError(502, "the desk's TYPESAFE_API_KEY was rejected by TypeSafe")
    if status == 422:
        return DeskError(422, f"TypeSafe rejected the request (422): {text[:300]}")
    return DeskError(502, f"TypeSafe call failed: {type(exc).__name__}" + (f" (HTTP {status})" if status else ""))
