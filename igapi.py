"""Thin wrapper around the Instagram Graph API Business Discovery endpoint.

Business Discovery lets YOUR Instagram professional account read public data
about any other Business/Creator account by username: profile info plus recent
posts with like/comment/view counts.
Docs: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery
"""
import json
import os
import time

import requests

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v27.0")
BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

PROFILE_FIELDS = "username,name,website,biography,followers_count,media_count"
# view_count / media_product_type are newer fields; if Meta rejects one we drop it and retry.
MEDIA_FIELDS = [
    "id", "caption", "media_type", "media_product_type", "permalink",
    "timestamp", "like_count", "comments_count", "view_count",
]
OPTIONAL_MEDIA_FIELDS = ["media_product_type", "view_count"]


class RateLimited(Exception):
    pass


class AccountUnavailable(Exception):
    """Username doesn't exist, is a personal account, is age-gated, or was renamed."""


class TokenError(Exception):
    pass


class IGClient:
    def __init__(self, ig_user_id=None, token=None, usage_ceiling=85):
        self.ig_user_id = ig_user_id or os.environ["IG_USER_ID"]
        self.token = token or os.environ["IG_ACCESS_TOKEN"]
        self.usage_ceiling = usage_ceiling  # stop when Meta says we've used this % of quota
        self.media_fields = list(MEDIA_FIELDS)
        self.calls = 0
        self.last_usage = {}

    # ---- rate limit bookkeeping -------------------------------------------------
    def _check_usage(self, resp):
        usage = {}
        for header in ("x-app-usage", "x-business-use-case-usage"):
            raw = resp.headers.get(header)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if header == "x-business-use-case-usage":
                # {"<id>": [{"call_count":..,"total_time":..,"total_cputime":..}]}
                for entries in data.values():
                    for e in entries:
                        for k in ("call_count", "total_time", "total_cputime"):
                            usage[k] = max(usage.get(k, 0), e.get(k, 0))
            else:
                for k, v in data.items():
                    usage[k] = max(usage.get(k, 0), v)
        self.last_usage = usage
        if usage and max(usage.values()) >= self.usage_ceiling:
            raise RateLimited(f"API usage at {max(usage.values())}% - stopping this run")

    # ---- calls ------------------------------------------------------------------
    def business_discovery(self, username, media_limit=12):
        username = username.strip().lstrip("@").lower()
        while True:
            fields = (
                f"business_discovery.username({username})"
                f"{{{PROFILE_FIELDS},media.limit({media_limit}){{{','.join(self.media_fields)}}}}}"
            )
            resp = requests.get(
                f"{BASE}/{self.ig_user_id}",
                params={"fields": fields, "access_token": self.token},
                timeout=30,
            )
            self.calls += 1
            body = resp.json() if resp.content else {}
            err = body.get("error")
            if not err:
                self._check_usage(resp)
                return body.get("business_discovery", {})

            code, sub, msg = err.get("code"), err.get("error_subcode"), err.get("message", "")
            # Field not supported -> drop an optional field and retry once per field
            droppable = [f for f in OPTIONAL_MEDIA_FIELDS if f in msg and f in self.media_fields]
            if code == 100 and droppable:
                self.media_fields.remove(droppable[0])
                continue
            if code in (4, 17, 32, 613) or "rate limit" in msg.lower():
                raise RateLimited(msg)
            if code == 190:
                raise TokenError(msg)
            if code == 110 or sub in (2207013,) or "Cannot find User" in msg or code == 100:
                raise AccountUnavailable(msg)
            raise RuntimeError(f"Graph API error for @{username}: {err}")

    def polite_sleep(self, seconds=1.0):
        time.sleep(seconds)
