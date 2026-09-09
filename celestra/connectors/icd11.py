"""WHO ICD-11 (ICD API).

Credential-gated. The missing-credential check happens BEFORE any network
call: an unauthenticated request would come back 401 and read like a broken
endpoint, when the truth is simply that ICD11_CLIENT_ID / ICD11_CLIENT_SECRET
are not configured.

The OAuth2 client-credentials token is cached in-process with its expiry, so a
run makes one token call, not one per query.
"""
from __future__ import annotations

import time
from typing import Any

from ..models import EvidenceOrigin, SourceRef
from ..settings import get_settings
from ._util import clean, clip, join_sections, strip_tags
from .base import ConnectorResult, RetrievalContext, describe_http_error, http

TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
RELEASE = "2026-01"
SEARCH_URL = f"https://id.who.int/icd/release/11/{RELEASE}/mms/search"
ENTITY_URL = "https://icd.who.int/browse/{release}/mms/en#/{entity_id}"

MISSING_CREDENTIALS = "credentials not configured"

# Module-level so every connector instance in a run shares one token.
_token: dict[str, Any] = {"value": "", "expires_at": 0.0}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "API-Version": "v2",
        "Accept": "application/json",
        "Accept-Language": "en",
    }


async def get_token() -> str:
    """Cached client-credentials token. Returns "" when unconfigured."""
    s = get_settings()
    if not (s.icd11_client_id and s.icd11_client_secret):
        return ""
    if _token["value"] and time.time() < _token["expires_at"]:
        return _token["value"]
    import base64

    basic = base64.b64encode(
        f"{s.icd11_client_id}:{s.icd11_client_secret}".encode()
    ).decode()
    payload = await http.request(
        "POST", TOKEN_URL,
        data={"grant_type": "client_credentials", "scope": "icdapi_access"},
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        use_cache=False,
    )
    token = clean(payload.get("access_token"))
    # Renew a minute early so a long run never uses an expiring token.
    _token.update({"value": token,
                   "expires_at": time.time() + float(payload.get("expires_in") or 3600) - 60})
    return token


class Icd11Connector:
    """ICD-11 MMS entity search."""

    source_id = "icd11"
    tier = 1
    origin = EvidenceOrigin.APPROVED_API
    source_name = "WHO ICD-11"
    required_credentials = ("ICD11_CLIENT_ID", "ICD11_CLIENT_SECRET")

    @staticmethod
    def configured() -> bool:
        s = get_settings()
        return bool(s.icd11_client_id and s.icd11_client_secret)

    async def search(self, query: str, token: str) -> dict:
        return await http.get_json(
            SEARCH_URL,
            params={"q": clean(query), "useFlexisearch": "false",
                    "flatResults": "true"},
            headers=_headers(token),
        )

    async def discover(self, ctx: RetrievalContext, limit: int) -> ConnectorResult:
        # Gate BEFORE calling out: a 401 would misreport a config gap as an outage.
        if not self.configured():
            return ConnectorResult.failure(self.source_id, MISSING_CREDENTIALS)
        started = time.perf_counter()
        calls = 0
        try:
            token = await get_token()
            calls += 1
            if not token:
                return ConnectorResult.failure(self.source_id, MISSING_CREDENTIALS)
            payload = await self.search(ctx.indication, token)
            calls += 1
            entities = payload.get("destinationEntities") or []
            refs = []
            for entity in entities[:limit]:
                title = strip_tags(clean(entity.get("title")))
                code = clean(entity.get("theCode"))
                entity_id = clean(entity.get("id")).rsplit("/", 1)[-1]
                matched = [strip_tags(clean(p.get("label")))
                           for p in entity.get("matchingPVs") or []][:6]
                body = join_sections({
                    "ICD-11 code": code,
                    "Title": title,
                    "Matching terms": "; ".join(matched),
                })
                refs.append(SourceRef(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    tier=self.tier,
                    url=clean(entity.get("id")) or
                        ENTITY_URL.format(release=RELEASE, entity_id=entity_id),
                    title=f"ICD-11 {code}: {title}".strip(),
                    organization="World Health Organization",
                    published=RELEASE,
                    identifiers={k: v for k, v in {
                        "icd11_code": code, "entity_id": entity_id, "release": RELEASE,
                    }.items() if v},
                    snippet=clip(body, 900),
                    raw={"entity": entity, "text": body},
                    origin=self.origin,
                ))
        except Exception as exc:  # noqa: BLE001 - a connector never raises
            return ConnectorResult.failure(self.source_id, describe_http_error(exc))
        return ConnectorResult(
            source_id=self.source_id, refs=refs, ok=True,
            reason="" if refs else "no results", calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
