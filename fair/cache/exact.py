import hashlib
import json
from datetime import datetime
from time import time

from sqlalchemy import delete, func, select

from fair.governor.policy import AdmissionDenied, admit_provider
from fair.providers.base import AuthenticationFailed
from fair.quality.engine import acceptable, evaluate
from fair.quality.version import ENGINE_VERSION
from fair.schemas.api import SolveResponse
from fair.schemas.db import CacheEntry, FeedbackEvent, ProviderQuotaState, TaskRequest
from fair.schemas.domain import NormalizedModelResponse


def canonical(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted(canonical(item) for item in value)
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


class ExactCache:
    def __init__(self, router, clock=time):
        self.router, self.clock = router, clock

    def key(self, request, profile):
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
            or router.settings.source_policy is not None
            or router.settings.benchmark_policy is not None
        ):
            return None
        # Preserve task whitespace and JSON array order: approximate equivalence is unsafe.
        payload = {
            "cache_version": 1,
            "engine": ENGINE_VERSION,
            "request": request.model_dump(exclude={"priority", "cache_mode", "cache_ttl_seconds"}),
            "profile": profile.model_dump(),
            "settings": router.settings.model_dump(),
            "thresholds": router.thresholds,
            "providers": [
                router.registry.providers[name].model_dump()
                for name in sorted(router.registry.providers)
            ],
        }
        encoded = json.dumps(
            canonical(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    def get(self, request, profile, request_id):
        key = self.key(request, profile)
        router, now = self.router, self.clock()
        if key is None or router.stopped:
            return None
        with router.sessions.begin() as session:
            entry = session.get(CacheEntry, (request.client_id, key))
            if entry is None:
                return None
            ttl = min(
                request.cache_ttl_seconds or router.settings.cache_ttl_seconds,
                router.settings.cache_ttl_seconds,
            )
            if request.cache_mode == "refresh" or not entry.created_at <= now < min(
                entry.expires_at, entry.created_at + ttl
            ):
                session.delete(entry)
                return None
            source = session.get(TaskRequest, entry.source_request_id)
            feedback = session.scalar(
                select(FeedbackEvent).where(FeedbackEvent.request_id == entry.source_request_id)
            )
            if (
                source is None
                or source.client_id != request.client_id
                or source.status != "ACCEPTED"
                or source.execution_kind != "PRIMARY"
                or not source.result_json
                or feedback is not None
                and (feedback.score < 50 or feedback.correction_hash is not None)
            ):
                session.delete(entry)
                return None
            try:
                original = SolveResponse.model_validate(source.result_json)
                spec = router.registry.providers[original.provider_id]
                admit_provider(spec)
                adapter = router.registry.adapters.get(spec.provider_id)
                if adapter is None:
                    return None
                if hasattr(adapter, "check_admission"):
                    adapter.check_admission()
                if (
                    original.status != "ACCEPTED"
                    or original.cache_hit
                    or original.output is None
                    or original.quality is None
                ):
                    raise ValueError()
                if session.get(ProviderQuotaState, spec.provider_id).security_blocked:
                    return None
                if not any(
                    model.model_id == original.model_id and model.active for model in spec.models
                ):
                    return None
                normalized = NormalizedModelResponse(
                    provider_id=original.provider_id,
                    model_id=original.model_id,
                    text=original.output,
                )
                quality = evaluate(request, profile, normalized)
                if not acceptable(quality, profile):
                    session.delete(entry)
                    return None
            except (ValueError, KeyError, AdmissionDenied, AuthenticationFailed):
                session.delete(entry)
                return None
            result = SolveResponse(
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
                cached_from_request_id=source.id,
            )
            row = session.get(TaskRequest, request_id)
            row.status, row.result_json = result.status, result.model_dump(mode="json")
            router.audit(
                session,
                request_id,
                request.client_id,
                "CACHE_HIT",
                {"source_request_id": source.id, "revalidated": True},
            )
            router.audit(
                session,
                request_id,
                request.client_id,
                "ACCEPTED",
                {
                    "reason_code": result.reason_code,
                    "attempts_count": 0,
                    "paid_inference_executed": False,
                },
            )
            return result

    def put(self, request, profile, result):
        key = self.key(request, profile)
        router, now = self.router, self.clock()
        if (
            key is None
            or router.stopped
            or result.status != "ACCEPTED"
            or result.cache_hit
            or result.execution_kind != "PRIMARY"
        ):
            return
        with router.sessions.begin() as session:
            session.execute(delete(CacheEntry).where(CacheEntry.expires_at <= now))
            existing = session.get(CacheEntry, (request.client_id, key))
            if existing is None:
                count = session.scalar(select(func.count()).select_from(CacheEntry))
                if count >= router.settings.cache_max_entries:
                    oldest = session.scalars(
                        select(CacheEntry)
                        .order_by(CacheEntry.created_at, CacheEntry.client_id, CacheEntry.key)
                        .limit(count - router.settings.cache_max_entries + 1)
                    )
                    for old in oldest:
                        session.delete(old)
                existing = CacheEntry(client_id=request.client_id, key=key)
                session.add(existing)
            existing.source_request_id = result.request_id
            existing.created_at = now
            existing.expires_at = now + min(
                request.cache_ttl_seconds or router.settings.cache_ttl_seconds,
                router.settings.cache_ttl_seconds,
            )
            router.audit(
                session,
                result.request_id,
                request.client_id,
                "CACHE_STORED",
                {"expires_at": existing.expires_at},
            )

    def clear(self, client_id):
        with self.router.sessions.begin() as session:
            result = session.execute(delete(CacheEntry).where(CacheEntry.client_id == client_id))
            self.router.audit(
                session, None, client_id, "CACHE_CLEARED", {"entries_removed": result.rowcount}
            )
            return {"entries_removed": result.rowcount}
