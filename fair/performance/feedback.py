import hashlib

from pydantic import Field, model_validator
from sqlalchemy import select

from fair.schemas.db import AuditEvent, FeedbackEvent, TaskRequest
from fair.schemas.domain import DTO


class FeedbackRequest(DTO):
    request_id: str = Field(min_length=1, max_length=36)
    accepted: bool | None = None
    rating: int | None = Field(default=None, ge=1, le=5, strict=True)
    reason: str | None = Field(default=None, max_length=1000)
    correction_text: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def has_rating(self):
        if self.accepted is None and self.rating is None:
            raise ValueError("Feedback needs an acceptance decision or rating")
        return self


class FeedbackDenied(Exception):
    def __init__(self, reason, status=409):
        self.reason, self.status = reason, status
        super().__init__(reason)


def report(row):
    return {
        "feedback_id": row.id,
        "request_id": row.request_id,
        "accepted": row.accepted,
        "rating": row.rating,
        "reason_hash": row.reason_hash,
        "correction_hash": row.correction_hash,
        "scope": "client_routing_preference",
    }


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest() if value is not None else None


class FeedbackRegistry:
    def __init__(self, sessions):
        self.sessions = sessions

    def submit(self, client_id, request):
        fingerprint = digest(request.model_dump_json())
        with self.sessions.begin() as session:
            task = session.scalar(
                select(TaskRequest).where(TaskRequest.id == request.request_id).with_for_update()
            )
            if task is None or task.client_id != client_id:
                raise FeedbackDenied("REQUEST_NOT_FOUND", 404)
            existing = session.scalar(
                select(FeedbackEvent).where(FeedbackEvent.request_id == task.id)
            )
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise FeedbackDenied("FEEDBACK_ALREADY_RECORDED")
                return report(existing)
            if task.status != "ACCEPTED" or task.execution_kind != "PRIMARY":
                raise FeedbackDenied("FEEDBACK_REQUIRES_ACCEPTED_PRIMARY_RESULT")
            result = task.result_json or {}
            if result.get("cache_hit"):
                raise FeedbackDenied("FEEDBACK_USE_ORIGINAL_REQUEST")
            if not result.get("provider_id") or not result.get("model_id"):
                raise FeedbackDenied("FEEDBACK_TARGET_UNAVAILABLE")
            if request.accepted is False:
                score = 0
            elif request.rating is not None:
                score = (request.rating - 1) * 25
                if request.accepted is True:
                    score = max(score, 25)
            else:
                score = 100
            row = FeedbackEvent(
                request_id=task.id,
                client_id=client_id,
                provider_id=result["provider_id"],
                model_id=result["model_id"],
                task_class=task.profile_json["task_class"],
                accepted=request.accepted,
                rating=request.rating,
                score=score,
                fingerprint=fingerprint,
                reason_hash=digest(request.reason),
                correction_hash=digest(request.correction_text),
            )
            session.add(row)
            session.flush()
            result = report(row)
            session.add(
                AuditEvent(
                    request_id=task.id,
                    actor_id=client_id,
                    event_type="FEEDBACK_RECORDED",
                    payload_json=result,
                )
            )
        return result

    def read(self, client_id, request_id):
        with self.sessions() as session:
            task = session.get(TaskRequest, request_id)
            if task is None or task.client_id != client_id:
                raise FeedbackDenied("REQUEST_NOT_FOUND", 404)
            row = session.scalar(
                select(FeedbackEvent).where(FeedbackEvent.request_id == request_id)
            )
            if row is None:
                raise FeedbackDenied("FEEDBACK_NOT_FOUND", 404)
            return report(row)
