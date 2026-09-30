from prometheus_client import CollectorRegistry, Histogram, generate_latest

SUBSYSTEM = "kaptain_decider"

registry = CollectorRegistry()

# Same buckets as the Go plugin's ExponentialBuckets(0.0001, 2, 14), so the caller-side and
# handler-side histograms can be read side by side.
_SECONDS = tuple(0.0001 * 2 ** power for power in range(14))

handler_duration = Histogram(f"{SUBSYSTEM}_handler_duration_seconds",
                             "Time spent computing policy scores, excluding HTTP handling.",
                             ["endpoint", "status"], buckets=_SECONDS, registry=registry)


def render() -> bytes:
    return generate_latest(registry)
