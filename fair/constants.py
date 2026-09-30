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


# A byte is not a token. Counting one as the other overstated a prompt by two to
# three times, and because the output budget shares the same window, raising budgets
# to 32768 made that hide models whose context was ample: a 131,072-token model was
# refused a request needing about 57,000.
#
# Three bytes per token is deliberately short of the four an English prompt usually
# runs at. It stays conservative where text is denser: CJK is three UTF-8 bytes and
# roughly one token per character, and packed JSON tokenizes worse than prose. The
# error that matters is the one that under-counts -- that routes a request the
# provider then refuses -- so this errs high and the caller's own margin sits on top.
BYTES_PER_TOKEN = 3


def estimated_tokens(text):
    """Tokens `text` is expected to occupy, from its UTF-8 length.

    Deliberately an estimate. No tokenizer is loaded: FAIR routes across models that
    do not share one, and the number is only ever compared against a context window
    to decide whether a route is worth trying.
    """
    return len(text.encode("utf-8")) // BYTES_PER_TOKEN + 1
