import re

from fair.schemas.api import SolveRequest
from fair.schemas.domain import TaskProfile


def profile_task(request: SolveRequest, thresholds: dict[str, float]) -> TaskProfile:
    task = request.task.lower()
    required = set(request.required_capabilities)
    task_class = request.task_type or "general"
    if re.search(r"```|\b(code|debug|compiler|python|javascript)\b|\.py\b", task):
        required.add("coding")
        task_class = "coding"
    if request.expected_schema is not None:
        required.add("structured_output")
        task_class = "extraction"
    grounding = request.freshness_required or bool(
        re.search(r"\b(latest|current|sources?|citations?|research)\b", task)
    )
    return TaskProfile(
        task_class=task_class,
        required_capabilities=required,
        # UTF-8 byte count is a conservative bound, not a tokenizer claim.
        context_tokens_estimate=len(request.task.encode("utf-8")) + 1024,
        minimum_quality_score=thresholds[request.quality_level],
        requires_grounding=grounding,
    )
