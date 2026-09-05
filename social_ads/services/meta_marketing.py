"""
Meta Graph + Marketing API client for Social Studio — BYOK edition.

Every call is authenticated with the *tenant's own* Page Access Token / Ad
Account, so all ad spend is billed to the tenant by Meta directly (Mouss Tec
bills only the flat monthly add-on fee).

Covers the three jobs the autopilot needs:
  1. PUBLISH   — a text/photo post to a Facebook Page, and a photo post to an
                 Instagram Business account (two-step container → publish).
  2. INSIGHTS  — read post-level and campaign-level performance metrics.
  3. ADS       — create a Campaign → Ad Set → Ad (Marketing API), and pause /
                 adjust a running campaign for the optimizer.

Design mirrors omnichannel/services/meta_api.py: bounded exponential-backoff
retry on transient errors, a hard per-request timeout, and a single typed
exception so callers never see a raw requests error.
"""
from __future__ import annotations

import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger("mouss_tec_core")

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.7
_TIMEOUT = 20  # ad calls are heavier than a WhatsApp text


class MetaMarketingError(Exception):
    """Any failed Meta Graph / Marketing API operation."""


def _graph_base() -> str:
    version = getattr(settings, "META_GRAPH_VERSION", "v19.0")
    return f"https://graph.facebook.com/{version}"


# ── low-level HTTP with retry ─────────────────────────────────────────
def _request(method: str, url: str, *, params=None, data=None, json_body=None) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.request(
                method, url, params=params, data=data, json=json_body, timeout=_TIMEOUT
            )
            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError:
                    return {"status": "ok"}
            # 4xx (not 429) → permanent, surface the Meta error message.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                msg = _extract_error(resp)
                logger.error("social_ads: Meta rejected %s %s (status=%d): %s",
                             method, _safe_url(url), resp.status_code, msg)
                raise MetaMarketingError(f"Meta API {resp.status_code}: {msg}")
            logger.warning("social_ads: transient Meta error %d (attempt=%d)",
                           resp.status_code, attempt)
            last_exc = MetaMarketingError(f"status={resp.status_code}")
        except requests.RequestException as exc:
            logger.warning("social_ads: network error (attempt=%d): %s", attempt, exc)
            last_exc = exc
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
    raise MetaMarketingError(f"Failed after {_MAX_ATTEMPTS} attempts: {last_exc}")


def _extract_error(resp) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", resp.text[:200]))
    except (ValueError, AttributeError):
        return resp.text[:200]


def _safe_url(url: str) -> str:
    return url.split("?", 1)[0]


# =====================================================================
# 1. PUBLISHING
# =====================================================================
def publish_facebook_post(*, access_token: str, page_id: str, message: str,
                          image_url: str = "", link: str = "") -> dict:
    """Publish to a Facebook Page. Photo post if image_url is given, else feed post.

    Returns the raw Meta response; a photo post returns {'id', 'post_id'} and a
    feed post returns {'id'}. Caller normalizes to a post id.
    """
    if not access_token:
        raise MetaMarketingError("Page access token is not configured")
    if not page_id:
        raise MetaMarketingError("Facebook page_id is not configured")
    body = (message or "").strip()
    if not body and not image_url:
        raise MetaMarketingError("Refusing to publish an empty post")

    if image_url:
        url = f"{_graph_base()}/{page_id}/photos"
        payload = {"url": image_url, "caption": body, "access_token": access_token}
        if not body:
            payload.pop("caption")
        return _request("POST", url, data=payload)

    url = f"{_graph_base()}/{page_id}/feed"
    payload = {"message": body, "access_token": access_token}
    if link:
        payload["link"] = link
    return _request("POST", url, data=payload)


def publish_instagram_post(*, access_token: str, ig_user_id: str, image_url: str,
                           caption: str = "") -> dict:
    """Two-step Instagram publish: create a media container, then publish it.

    Instagram requires a publicly reachable image_url — a caption-only post is
    not supported by the API, so image_url is mandatory here.
    """
    if not access_token:
        raise MetaMarketingError("Page access token is not configured")
    if not ig_user_id:
        raise MetaMarketingError("Instagram account id is not configured")
    if not image_url:
        raise MetaMarketingError("Instagram requires an image_url")

    # Step 1 — container
    create = _request(
        "POST", f"{_graph_base()}/{ig_user_id}/media",
        data={"image_url": image_url, "caption": (caption or "").strip(), "access_token": access_token},
    )
    creation_id = create.get("id")
    if not creation_id:
        raise MetaMarketingError(f"Instagram container creation returned no id: {create}")

    # Step 2 — publish (small settle delay; Meta processes the container async)
    time.sleep(1.5)
    return _request(
        "POST", f"{_graph_base()}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
    )


def fetch_page_posts(*, access_token: str, page_id: str, limit: int = 25) -> list[dict]:
    """List the page's recent published posts (for backfill/analysis).

    Returns a list of normalized dicts: {id, message, created_time, permalink,
    picture, likes, comments, shares}. Insights (reach/impressions/clicks) are
    fetched separately per post via fetch_post_insights. Returns [] on failure.
    """
    if not access_token or not page_id:
        return []
    try:
        data = _request(
            "GET", f"{_graph_base()}/{page_id}/posts",
            params={
                "fields": (
                    "id,message,created_time,permalink_url,full_picture,"
                    "shares,likes.summary(true),comments.summary(true)"
                ),
                "limit": max(1, min(int(limit), 100)),
                "access_token": access_token,
            },
        )
    except MetaMarketingError as exc:
        logger.info("social_ads: fetch_page_posts failed for %s: %s", page_id, exc)
        return []

    out = []
    for row in data.get("data", []) or []:
        out.append({
            "id": row.get("id", ""),
            "message": row.get("message", "") or "",
            "created_time": row.get("created_time", ""),
            "permalink": row.get("permalink_url", "") or "",
            "picture": row.get("full_picture", "") or "",
            "likes": int((row.get("likes", {}).get("summary", {}) or {}).get("total_count", 0)),
            "comments": int((row.get("comments", {}).get("summary", {}) or {}).get("total_count", 0)),
            "shares": int((row.get("shares", {}) or {}).get("count", 0)),
        })
    return out


def get_post_permalink(*, access_token: str, post_id: str) -> str:
    try:
        data = _request("GET", f"{_graph_base()}/{post_id}",
                        params={"fields": "permalink_url", "access_token": access_token})
        return data.get("permalink_url", "")
    except MetaMarketingError:
        return ""


# =====================================================================
# 2. INSIGHTS
# =====================================================================
def fetch_post_insights(*, access_token: str, post_id: str) -> dict:
    """Return normalized engagement metrics for one Facebook post.

    Uses the summary edges (likes/comments/shares counts) + insights metrics
    (impressions, reach, clicks). Missing metrics default to 0 so callers never
    KeyError on a brand-new post with no data yet.
    """
    out = {"reach": 0, "impressions": 0, "likes": 0, "comments": 0, "shares": 0, "clicks": 0}
    try:
        data = _request(
            "GET", f"{_graph_base()}/{post_id}",
            params={
                "fields": "shares,likes.summary(true),comments.summary(true)",
                "access_token": access_token,
            },
        )
        out["likes"] = int((data.get("likes", {}).get("summary", {}) or {}).get("total_count", 0))
        out["comments"] = int((data.get("comments", {}).get("summary", {}) or {}).get("total_count", 0))
        out["shares"] = int((data.get("shares", {}) or {}).get("count", 0))
    except MetaMarketingError as exc:
        logger.info("social_ads: post edges fetch failed for %s: %s", post_id, exc)

    try:
        ins = _request(
            "GET", f"{_graph_base()}/{post_id}/insights",
            params={
                "metric": "post_impressions,post_impressions_unique,post_clicks",
                "access_token": access_token,
            },
        )
        for row in ins.get("data", []) or []:
            name = row.get("name")
            values = row.get("values") or [{}]
            val = int(values[0].get("value", 0) or 0)
            if name == "post_impressions":
                out["impressions"] = val
            elif name == "post_impressions_unique":
                out["reach"] = val
            elif name == "post_clicks":
                out["clicks"] = val
    except MetaMarketingError as exc:
        logger.info("social_ads: post insights fetch failed for %s: %s", post_id, exc)
    return out


def fetch_campaign_insights(*, access_token: str, campaign_id: str) -> dict:
    """Return normalized spend/performance metrics for one ad campaign."""
    out = {"spend": 0.0, "reach": 0, "impressions": 0, "clicks": 0, "ctr": 0.0, "results": 0}
    try:
        data = _request(
            "GET", f"{_graph_base()}/{campaign_id}/insights",
            params={
                "fields": "spend,reach,impressions,clicks,ctr,actions",
                "access_token": access_token,
            },
        )
        rows = data.get("data") or []
        if rows:
            row = rows[0]
            out["spend"] = float(row.get("spend", 0) or 0)
            out["reach"] = int(row.get("reach", 0) or 0)
            out["impressions"] = int(row.get("impressions", 0) or 0)
            out["clicks"] = int(row.get("clicks", 0) or 0)
            out["ctr"] = float(row.get("ctr", 0) or 0)
            # "results" ≈ the most relevant conversion action; sum link clicks /
            # leads / messaging conversations if present.
            for act in row.get("actions", []) or []:
                if act.get("action_type") in (
                    "onsite_conversion.messaging_conversation_started_7d",
                    "lead", "link_click", "offsite_conversion.fb_pixel_lead",
                ):
                    out["results"] += int(float(act.get("value", 0) or 0))
    except MetaMarketingError as exc:
        logger.info("social_ads: campaign insights fetch failed for %s: %s", campaign_id, exc)
    return out


# =====================================================================
# 3. ADS (Marketing API)
# =====================================================================
def _norm_ad_account(ad_account_id: str) -> str:
    aid = (ad_account_id or "").strip()
    return aid if aid.startswith("act_") else f"act_{aid}"


def create_campaign(*, access_token: str, ad_account_id: str, name: str,
                    objective: str, status: str = "PAUSED") -> str:
    """Create a campaign (starts PAUSED so nothing spends before review). Returns its id."""
    acct = _norm_ad_account(ad_account_id)
    data = _request(
        "POST", f"{_graph_base()}/{acct}/campaigns",
        data={
            "name": name,
            "objective": objective,
            "status": status,
            "special_ad_categories": "[]",
            "access_token": access_token,
        },
    )
    cid = data.get("id")
    if not cid:
        raise MetaMarketingError(f"Campaign creation returned no id: {data}")
    return cid


def create_adset(*, access_token: str, ad_account_id: str, campaign_id: str, name: str,
                 daily_budget_minor: int, targeting: dict, optimization_goal: str,
                 billing_event: str = "IMPRESSIONS", status: str = "PAUSED") -> str:
    """Create an ad set under a campaign. `daily_budget_minor` is in minor units
    (e.g. piastres/cents). Returns the ad set id."""
    import json as _json
    acct = _norm_ad_account(ad_account_id)
    data = _request(
        "POST", f"{_graph_base()}/{acct}/adsets",
        data={
            "name": name,
            "campaign_id": campaign_id,
            "daily_budget": int(daily_budget_minor),
            "billing_event": billing_event,
            "optimization_goal": optimization_goal,
            "targeting": _json.dumps(targeting),
            "status": status,
            "access_token": access_token,
        },
    )
    sid = data.get("id")
    if not sid:
        raise MetaMarketingError(f"Ad set creation returned no id: {data}")
    return sid


def create_ad_from_post(*, access_token: str, ad_account_id: str, adset_id: str,
                        page_id: str, post_id: str, name: str, status: str = "PAUSED") -> str:
    """Create an ad that boosts an existing organic Page post. Returns the ad id."""
    import json as _json
    acct = _norm_ad_account(ad_account_id)
    # object_story_id links the creative to the already-published page post.
    creative = _request(
        "POST", f"{_graph_base()}/{acct}/adcreatives",
        data={
            "name": f"{name} — creative",
            "object_story_id": post_id if "_" in post_id else f"{page_id}_{post_id}",
            "access_token": access_token,
        },
    )
    creative_id = creative.get("id")
    if not creative_id:
        raise MetaMarketingError(f"Ad creative creation returned no id: {creative}")

    data = _request(
        "POST", f"{_graph_base()}/{acct}/ads",
        data={
            "name": name,
            "adset_id": adset_id,
            "creative": _json.dumps({"creative_id": creative_id}),
            "status": status,
            "access_token": access_token,
        },
    )
    ad_id = data.get("id")
    if not ad_id:
        raise MetaMarketingError(f"Ad creation returned no id: {data}")
    return ad_id


def set_campaign_status(*, access_token: str, campaign_id: str, status: str) -> dict:
    """ACTIVE / PAUSED a campaign (used by the launcher and the optimizer)."""
    return _request("POST", f"{_graph_base()}/{campaign_id}",
                    data={"status": status, "access_token": access_token})


def set_adset_budget(*, access_token: str, adset_id: str, daily_budget_minor: int) -> dict:
    """Adjust an ad set's daily budget (the optimizer shifts spend to winners)."""
    return _request("POST", f"{_graph_base()}/{adset_id}",
                    data={"daily_budget": int(daily_budget_minor), "access_token": access_token})
