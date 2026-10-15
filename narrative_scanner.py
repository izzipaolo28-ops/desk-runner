"""Read-only X + news narrative discovery for the JEV dashboard.
Requires X_BEARER_TOKEN for X search; public Google News RSS works without credentials.
This module never submits trades or touches wallet keys.
"""
from __future__ import annotations
import email.utils
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import quote_plus

import requests

WINDOW_HOURS = 12
TIMEOUT = 12
CATEGORIES = {
    "viral animals": ["viral animal meme", "animal goes viral", "pet viral video"],
    "AI launches": ["AI agent launch", "new AI model launch", "AI tool viral"],
    "celebrity moments": ["celebrity viral moment", "celebrity meme trending"],
    "political events": ["politics viral moment", "election political meme"],
    "meme formats": ["new meme format", "viral meme template", "internet meme trend"],
}
KILL_RULES = {
    "viral animals": "Original clip/source cannot be verified; engagement is mostly reposts; attention fades within hours.",
    "AI launches": "No working public demo or credible launch source; engagement is mostly promotional/bot-like.",
    "celebrity moments": "Claim is unverified, clipped out of context, or the celebrity/team publicly denies it.",
    "political events": "No confirmation from reputable reporting; story is corrected, stale, or engagement is partisan noise only.",
    "meme formats": "No independent creators using the format; trend is confined to one account or one platform.",
}


def _published(item):
    for key in ("created_at", "pubDate", "published"):
        value = item.get(key) if isinstance(item, dict) else None
        if not value:
            continue
        try:
            if key == "created_at":
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            else:
                dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def _news(query, cutoff):
    url = "https://news.google.com/rss/search?q=" + quote_plus(query + " when:12h") + "&hl=en-US&gl=US&ceid=US:en"
    try:
        response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "JEV-NarrativeScanner/1.0"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
        out = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = _published({"pubDate": item.findtext("pubDate")})
            if title and link and pub and pub >= cutoff:
                out.append({"text": title, "url": link, "created_at": pub.isoformat(), "source": "news"})
        return out
    except Exception:
        return []


def _x_posts(query, cutoff, token):
    if not token:
        return []
    url = "https://api.x.com/2/tweets/search/recent"
    params = {"query": f'({query}) -is:retweet', "max_results": 25,
              "tweet.fields": "created_at,public_metrics,author_id,lang", "expansions": "author_id",
              "user.fields": "username,public_metrics"}
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT,
                                headers={"Authorization": "Bearer " + token, "User-Agent": "JEV-NarrativeScanner/1.0"})
        if response.status_code == 429:
            return []
        response.raise_for_status()
        payload = response.json()
        users = {u.get("id"): u for u in payload.get("includes", {}).get("users", [])}
        out = []
        for post in payload.get("data", []):
            pub = _published(post)
            if not pub or pub < cutoff:
                continue
            metrics = post.get("public_metrics") or {}
            user = users.get(post.get("author_id"), {})
            out.append({"text": post.get("text", ""), "url": "https://x.com/" + user.get("username", "i") + "/status/" + post.get("id", ""),
                        "created_at": pub.isoformat(), "source": "x", "author": user.get("username", ""),
                        "engagement": sum(int(metrics.get(k, 0) or 0) for k in ("like_count", "reply_count", "retweet_count", "quote_count"))})
        return out
    except Exception:
        return []


def _keywords(text):
    words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,15}", text.lower())
    stop = set("the and for with from that this have into about after before says said their they them your viral trend meme news launch latest today new video official more what when where why how just not are was were has had its over under out you all can will ai solana token coin crypto breaking report says live".split())
    return [w for w in words if w not in stop and not w.startswith("http")]


def _ticker_ideas(text):
    words = [w.upper() for w in _keywords(text) if w.isalpha() and len(w) >= 3]
    picks = []
    for w in words:
        if w not in picks:
            picks.append(w[:8])
        if len(picks) == 3:
            break
    # Clearly label these as naming ideas, not existing or available tickers.
    while len(picks) < 3:
        picks.append(["EARLY", "VIBE", "LORE"][len(picks)])
    return picks[:3]


def scan_narratives():
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - WINDOW_HOURS * 3600
    cutoff_dt = datetime.fromtimestamp(cutoff, timezone.utc)
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    records = []
    for category, queries in CATEGORIES.items():
        for query in queries:
            records.extend(_news(query, cutoff_dt))
            records.extend(_x_posts(query, cutoff_dt, token))
            time.sleep(0.05)
    # De-duplicate identical URLs and collapse each category's evidence into one candidate.
    unique = {}
    for item in records:
        unique[item.get("url")] = item
    records = list(unique.values())
    scored = []
    for category, queries in CATEGORIES.items():
        matching = [r for r in records if any(w in r.get("text", "").lower() for q in queries for w in q.lower().split() if len(w) > 3)]
        # If generic query matching is too broad, category-specific news/X query results still count.
        if not matching:
            continue
        words = Counter(w for r in matching for w in _keywords(r.get("text", "")))
        common = [w for w, _ in words.most_common(4)]
        sample = max(matching, key=lambda r: len(r.get("text", ""))).get("text", "")
        latest = max((_published(r) for r in matching if _published(r)), default=now)
        sources = len(set(r.get("source") for r in matching))
        authors = len(set(r.get("author") for r in matching if r.get("author")))
        engagement = sum(int(r.get("engagement", 0) or 0) for r in matching)
        age_hours = max(0, (now - latest).total_seconds() / 3600)
        # Score prioritizes fresh signals and independent corroboration, not raw hype alone.
        strength = min(10, round(1 + min(3, len(matching) / 5) + min(2, authors / 8) + min(2, sources) + min(2, engagement / 5000), 1))
        early = max(1, min(10, round(10 - age_hours * 0.7 - max(0, len(matching)-8) * 0.25)))
        rank_score = round(0.55 * early + 0.45 * strength, 2)
        evidence = sorted(matching, key=lambda r: _published(r) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:5]
        core = (" ".join(common[:3]).title() if common else category.title()) + " — " + sample[:180]
        scored.append({"category": category, "core_meme": core, "ticker_ideas": _ticker_ideas(" ".join(common) or category),
                       "early_score": early, "strength_score": strength, "rank_score": rank_score,
                       "evidence_count": len(matching), "x_posts": sum(r.get("source") == "x" for r in matching),
                       "news_items": sum(r.get("source") == "news" for r in matching), "kill_conditions": KILL_RULES[category],
                       "evidence": [{"text": r.get("text", "")[:240], "url": r.get("url"), "source": r.get("source"), "created_at": r.get("created_at")} for r in evidence]})
    scored.sort(key=lambda r: (r["rank_score"], r["strength_score"], r["early_score"]), reverse=True)
    return {"generated_at": now.isoformat(), "window_hours": WINDOW_HOURS, "x_configured": bool(token),
            "note": "Narrative signals are research leads, not token listings or trade signals. Tickers are naming ideas only.",
            "narratives": scored[:5], "sources_seen": len(records)}
