SECONDS_IN_DAY = 86400
SECONDS_IN_YEAR = 365 * SECONDS_IN_DAY


def completion_deadline(max_output_tokens, *, base_seconds, tokens_per_second, ceiling_seconds):
    """Wall-clock budget for one completion of the requested size.

    A fixed budget is a bet that a completion of any size arrives within it. That
    bet holds for a few hundred tokens and fails for a few thousand: the router and
    the adapter each enforced one, so a large request was cancelled twice over
    before a model had finished writing. Scaling with the requested budget replaces
    the bet with an assumption that is written down, configurable, and identical on
    both sides of the call.

    tokens_per_second is an assumption about free-tier throughput, not a measurement,
    so ceiling_seconds bounds what any single request may cost however large it is.
    """
    if max_output_tokens <= 0 or tokens_per_second <= 0:
        return min(base_seconds, ceiling_seconds)
    return min(base_seconds + max_output_tokens / tokens_per_second, ceiling_seconds)
