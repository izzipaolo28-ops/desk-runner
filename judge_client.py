# judge_client.py
"""Thin client for the desk's POST /judge. One call per token (the whole question set at once); the raw
answers come back untouched and every threshold stays with the caller.

Retries follow the desk's contract: back off on 429 (desk cap) and 503 (TypeSafe 429/529, mapped by the
desk), honouring Retry-After. Never retry 401, 413 or 422: the request is wrong and will stay wrong.
Never retry a timeout either: the desk may already have asked TypeSafe, and you would pay twice.
"""
import logging
import os
import time

import requests
from urllib3.exceptions import NewConnectionError

log = logging.getLogger(__name__)
RETRY = {429, 503}
MAX_TRIES = 3
MAX_WAIT = 30.0


def _endpoint(url: str) -> str:
    """Accept the bare tunnel address or the full /judge URL; refuse plain http except for localhost."""
    url = url.strip().rstrip("/")
    if not url.endswith("/judge"):
        url += "/judge"
    local = url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
    if not (url.startswith("https://") or local):
        raise RuntimeError("JUDGE_URL must start with https:// (plain http only for localhost): "
                           "otherwise DESK_SECRET travels in clear text")
    return url


URL, SECRET = _endpoint(os.environ["JUDGE_URL"]), os.environ["DESK_SECRET"]
_session = requests.Session()  # keeps the TLS connection to the tunnel open between calls


class JudgeError(RuntimeError):
    """Raised for every failure; a RuntimeError, so existing `except RuntimeError` keeps working.
    `status` is the desk's HTTP status when there was one (422 = the question is wrong: stop the cycle)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _detail(r) -> str:
    try:
        body = r.json()
        text = str(body.get("detail", body)) if isinstance(body, dict) else str(body)
    except ValueError:
        text = r.text or ""
    return text.replace(SECRET, "[redacted]")[:300] if SECRET else text[:300]


def _wait(r, attempt: int) -> float:
    try:
        return min(float(r.headers.get("Retry-After")), MAX_WAIT)
    except (TypeError, ValueError):
        return min(2.0 * 2 ** attempt, MAX_WAIT)


def _never_sent(exc: requests.ConnectionError) -> bool:
    """True only when the TCP connection was never made (DNS failure, refused, connect timeout), so the
    desk cannot have seen the request. A reset after sending might have been processed and billed."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(reason, NewConnectionError)


def judge(question_set: str, state: dict) -> dict:
    """Returns {"model", "answers", "usage"} exactly as the desk sent them. Log "model": aliases move."""
    for attempt in range(MAX_TRIES):
        try:
            r = _session.post(URL, timeout=30, headers={"Authorization": f"Bearer {SECRET}"},
                              json={"question_set": question_set, "state": state})
        except requests.ConnectionError as exc:  # includes ConnectTimeout
            if _never_sent(exc) and attempt + 1 < MAX_TRIES:
                time.sleep(2.0 * 2 ** attempt)
                continue
            raise JudgeError(f"cannot reach the desk for {question_set}: {type(exc).__name__}") from None
        except requests.Timeout:  # read timeout: the desk may have done the work already
            raise JudgeError(f"desk timed out on {question_set} (not retried, to avoid paying twice)") from None

        if r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                raise JudgeError(f"desk answered {question_set} with non-JSON") from None
            if not isinstance(body, dict) or "answers" not in body:
                raise JudgeError(f"desk answered {question_set} without answers")
            log.info("judge %s model=%s usage=%s", question_set, body.get("model"), body.get("usage"))
            return body
        if r.status_code == 422:
            raise JudgeError(f"desk rejected {question_set} (422, not retried): {_detail(r)}", 422)
        if r.status_code in RETRY and attempt + 1 < MAX_TRIES:
            time.sleep(_wait(r, attempt))
            continue
        if r.status_code == 401:
            raise JudgeError("desk rejected DESK_SECRET (401)", 401)
        raise JudgeError(f"desk returned HTTP {r.status_code} for {question_set}: {_detail(r)}", r.status_code)
    raise JudgeError(f"desk still busy after {MAX_TRIES} tries for {question_set}")  # pragma: no cover
