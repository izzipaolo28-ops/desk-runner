"""The desk: holds the TypeSafe key so the bots never do. Bots authenticate with DESK_SECRET.

    POST /judge   {"question_set": "<set>", "state": {...}}   Authorization: Bearer <DESK_SECRET>
                  sets (questions.py): market, solana, bsc, robinhood, social, pick   (the desk bots)
                                       token, funnel_pick                              (memecoin-monitor)
    GET  /health  liveness only (no auth, reveals nothing)

Run:  cd desk && sh run.sh            (installs, starts the desk on 127.0.0.1:8080, opens a Cloudflare tunnel)
  or: cd desk && uvicorn judge:app --host 127.0.0.1 --port 8080
Env:  DESK_SECRET (>= 32 chars, for the bots)   TYPESAFE_API_KEY (read by the SDK itself)
      DESK_MAX_REQUESTS_PER_MIN (default 600)
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typesafe_sdk import AsyncTypeSafeClient

import core
from questions import SETS

DESK_SECRET = core.load_secret(os.environ)   # for the bots. NOT the TypeSafe key.
CAP = core.RequestCap(core.cap_from_env(os.environ))


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncTypeSafeClient() as client:   # reads TYPESAFE_API_KEY itself; closed on shutdown
        app.state.client = client
        yield


# no /docs, /redoc or /openapi.json on a server that holds an API key
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class Ask(BaseModel):
    question_set: str
    state: dict


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/judge")
async def judge(ask: Ask, authorization: str = Header("")):
    try:
        core.check_auth(authorization, DESK_SECRET)          # first: nothing else is revealed without it
        CAP.check()                                          # then the spend cap
        core.check_size(ask.state)
        qs = core.build_questions(ask.question_set, ask.state, SETS)
    except core.DeskError as e:
        raise HTTPException(e.status, e.detail, headers=e.headers) from None

    try:
        r = await app.state.client.system_one(state=ask.state, questions=qs)
        raw = r.raw_http_response.json()                     # documented way to get the raw API answer
        return core.passthrough(raw)                         # raw answers out. never flattened, never thresholded.
    except core.DeskError as e:
        raise HTTPException(e.status, e.detail, headers=e.headers) from None
    except Exception as exc:
        e = core.map_error(exc, redact=os.environ.get("TYPESAFE_API_KEY", "").strip())
        raise HTTPException(e.status, e.detail, headers=e.headers) from None
