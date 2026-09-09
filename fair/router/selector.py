from fair.governor.policy import AdmissionDenied, admit_provider

PRIVACY = {
    name: index for index, name in enumerate(["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"])
}


class Selector:
    def __init__(self, registry, quota, settings, performance, benchmarks):
        self.registry = registry
        self.quota = quota
        self.settings = settings
        self.performance = performance
        self.benchmarks = benchmarks

    def candidates(self, request, profile, tried, benchmark_checks=None, eligible=None):
        candidates = []
        for spec in self.registry.providers.values():
            try:
                admit_provider(spec)
            except AdmissionDenied:
                continue
            if spec.provider_id not in self.registry.adapters or not self.quota.available(spec):
                continue
            if PRIVACY[request.privacy_class] > PRIVACY[spec.max_data_class]:
                continue
            for model in spec.models:
                if not model.active or (spec.provider_id, model.model_id) in tried:
                    continue
                if not profile.required_capabilities <= model.capabilities:
                    continue
                if profile.context_tokens_estimate > model.context_window:
                    continue
                if eligible is not None and not eligible(spec, model):
                    continue
                quality_prior = None
                if self.settings.benchmark_policy is not None:
                    check = self.benchmarks.assess(
                        request, profile, spec, model, self.settings.benchmark_policy
                    )
                    if benchmark_checks is not None:
                        benchmark_checks[(spec.provider_id, model.model_id)] = check
                    if check.state != "PASSED":
                        continue
                    quality_prior = check.conservative_score / 100
                quality, reliability = self.performance.scores(
                    spec.provider_id,
                    model.model_id,
                    profile.task_class,
                    quality_prior=quality_prior,
                )
                remaining = self.quota.remaining(spec)
                headroom = remaining / spec.request_limit if spec.request_limit else 0.5
                score = (
                    self.settings.quality_weight * quality
                    + self.settings.quota_weight * headroom
                    + self.settings.reliability_weight * reliability
                )
                candidates.append((score, spec, model))
        return sorted(
            candidates, key=lambda item: (-item[0], item[1].provider_id, item[2].model_id)
        )
