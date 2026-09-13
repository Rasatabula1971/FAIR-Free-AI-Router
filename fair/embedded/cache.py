"""In-memory LRU cache — same key derivation, OrderedDict instead of SQL."""

import hashlib
import json
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal
from time import time

from fair.governor.policy import AdmissionDenied, admit_provider
from fair.providers.base import AuthenticationFailed
from fair.quality.engine import acceptable, evaluate
from fair.quality.version import ENGINE_VERSION
from fair.schemas.api import SolveResponse
from fair.schemas.domain import NormalizedModelResponse


def _canonical(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted(_canonical(item) for item in value)
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


class MemoryCache:
    def __init__(self, router, clock=time):
        self.router = router
        self.clock = clock
        self._entries: OrderedDict[str, dict] = OrderedDict()

    def _key(self, request, profile):
        router = self.router
        if (
            not router.settings.cache_enabled
            or request.cache_mode == "bypass"
            or request.validation is None
            or request.validation.kind not in {"arithmetic", "reference_json"}
            or request.freshness_required
            or profile.requires_grounding
            or request.cross_check_required
            or request.quality_level == "high_impact_support"
            or request.evidence
            or request.source_policy is not None
        ):
            return None
        payload = {
            "cache_version": 1,
            "engine": ENGINE_VERSION,
            "request": request.model_dump(exclude={"priority", "cache_mode", "cache_ttl_seconds"}),
            "profile": profile.model_dump(),
        }
        encoded = json.dumps(
            _canonical(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return request.client_id + ":" + hashlib.sha256(encoded.encode()).hexdigest()

    def get(self, request, profile, request_id):
        key = self._key(request, profile)
        if key is None or self.router.stopped:
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        now = self.clock()
        ttl = min(
            request.cache_ttl_seconds or self.router.settings.cache_ttl_seconds,
            self.router.settings.cache_ttl_seconds,
        )
        if request.cache_mode == "refresh" or not entry["created_at"] <= now < min(
            entry["expires_at"], entry["created_at"] + ttl
        ):
            del self._entries[key]
            return None
        original = entry["result"]
        try:
            spec = self.router.registry.providers[original.provider_id]
            admit_provider(spec)
            adapter = self.router.registry.adapters.get(spec.provider_id)
            if adapter is None:
                return None
            if hasattr(adapter, "check_admission"):
                adapter.check_admission()
            if original.status != "ACCEPTED" or original.output is None or original.quality is None:
                raise ValueError()
            if not any(m.model_id == original.model_id and m.active for m in spec.models):
                return None
            normalized = NormalizedModelResponse(
                provider_id=original.provider_id, model_id=original.model_id, text=original.output
            )
            quality = evaluate(request, profile, normalized)
            if not acceptable(quality, profile):
                del self._entries[key]
                return None
        except (ValueError, KeyError, AdmissionDenied, AuthenticationFailed):
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return SolveResponse(
            request_id=request_id,
            status="ACCEPTED",
            reason_code="EXACT_CACHE_HIT",
            attempts=[],
            minimum_required=profile.minimum_quality_score,
            best_quality_score=quality.overall_score,
            verification_state=quality.verification_state,
            quality=quality,
            output=original.output,
            provider_id=original.provider_id,
            model_id=original.model_id,
            cache_hit=True,
            cached_from_request_id=original.request_id,
        )

    def put(self, request, profile, result):
        key = self._key(request, profile)
        if (
            key is None
            or self.router.stopped
            or result.status != "ACCEPTED"
            or result.cache_hit
            or result.execution_kind != "PRIMARY"
        ):
            return
        now = self.clock()
        self._entries[key] = {
            "result": result.model_copy(deep=True),
            "created_at": now,
            "expires_at": now
            + min(
                request.cache_ttl_seconds or self.router.settings.cache_ttl_seconds,
                self.router.settings.cache_ttl_seconds,
            ),
        }
        self._entries.move_to_end(key)
        while len(self._entries) > self.router.settings.cache_max_entries:
            self._entries.popitem(last=False)

    def clear(self, client_id):
        keys = [k for k in self._entries if k.startswith(client_id + ":")]
        for k in keys:
            del self._entries[k]
        return {"entries_removed": len(keys)}
