"""Environment configuration, with the same units as the Go plugin.

Durations accept a Go duration string ("2s", "500ms") or a bare number of seconds ("2").
Both deployments must be configurable the same way, or a value copied from one manifest to
the other stops meaning what it says. An unparseable value raises at startup: silently
falling back to a default would leave a run believing it used a setting it never had.
"""

import os
import re

_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")


def parse_duration(text: str) -> float:
    """A Go duration string, or a bare number of seconds, as seconds."""
    text = str(text).strip()
    if not text:
        raise ValueError("empty duration")

    try:
        return float(text)
    except ValueError:
        pass

    body = text[1:] if text.startswith(("+", "-")) else text
    parts = _PART.findall(body)
    if not parts or "".join(value + unit for value, unit in parts) != body:
        raise ValueError(f"{text!r} is neither a duration (\"2s\") nor a number of seconds (\"2\")")

    seconds = sum(float(value) * _UNITS[unit] for value, unit in parts)
    return -seconds if text.startswith("-") else seconds


def duration(key: str, fallback: str) -> float:
    """A positive duration from the environment, in seconds."""
    text = os.getenv(key) or fallback
    try:
        value = parse_duration(text)
    except ValueError as exc:
        raise RuntimeError(f"{key}={text!r}: {exc}") from None
    if value <= 0:
        raise RuntimeError(f"{key}={text!r} must be positive")
    return value
