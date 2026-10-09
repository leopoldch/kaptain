#!/usr/bin/env python3
"""Score result folders. One folder: writes its report.md. Several: prints one comparison table.

Duration error is a factor: max(predicted / measured, measured / predicted).
1.0 is perfect, 2.0 means twice too long or twice too short.

Usage: python3 report.py results/RUN [results/OTHER_RUN ...]
"""

import json
from pathlib import Path
import statistics
import sys

TAGS = ["cpu", "memory", "disk", "network", "wait", "unknown"]
LEVELS = ["pod", "pod_image", "pod_image_node"]


def read(folder):
    folder = Path(folder)
    rows = [json.loads(line) for line in (folder / "responses.jsonl").read_text().splitlines()]
    references = json.loads((folder / "references.json").read_text())
    runs = json.loads((folder / "measurements.json").read_text())["cases"]
    measured = {case: statistics.median(run["seconds"] for run in r) for case, r in runs.items()}
    settings = json.loads((folder / "settings.json").read_text())
    return rows, references, measured, settings


def factor(predicted, measured):
    if predicted is None:
        return None
    if predicted <= 0:
        return float("inf")
    return max(predicted / measured, measured / predicted)


def percent(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "-"


def score(folder, level, timer=None):
    """timer=True/False keeps only cases whose spec states / does not state the duration."""
    rows, references, measured, _ = read(folder)
    rows = [r for r in rows if r["level"] == level and (timer is None or references[r["case"]]["timer"] == timer)]
    valid = [r for r in rows if r["prediction"]]
    factors = [factor(r["prediction"]["duration_seconds"], measured[r["case"]]) for r in valid]
    answered = [f for f in factors if f is not None]
    s = {"calls": len(rows), "valid": len(valid),
         "work_exact": sum(set(r["prediction"]["work_types"]) == set(references[r["case"]]["work_types"]) for r in valid),
         "estimates": len(answered), "no_estimate": len(factors) - len(answered),
         "median_factor": statistics.median(answered) if answered else None,
         "within_1_25": sum(f <= 1.25 for f in answered), "within_2": sum(f <= 2 for f in answered),
         "tags": {}}
    for tag in TAGS:
        truth = [tag in references[r["case"]]["work_types"] for r in valid]
        guess = [tag in r["prediction"]["work_types"] for r in valid]
        s["tags"][tag] = {"tp": sum(t and g for t, g in zip(truth, guess)),
                          "fp": sum(g and not t for t, g in zip(truth, guess)),
                          "fn": sum(t and not g for t, g in zip(truth, guess))}
    times = sorted(r["call_ms"] for r in valid)
    s["median_ms"] = statistics.median(times) if times else None
    s["p90_ms"] = times[int(0.9 * (len(times) - 1))] if times else None
    s["input_tokens"] = statistics.mean(r["input_tokens"] for r in valid) if valid else None
    s["output_tokens"] = statistics.mean(r["output_tokens"] for r in valid) if valid else None
    s["usd"] = sum(r.get("usd", 0) for r in rows)
    return s


def table(folders):
    lines = ["| Model | Level | Valid | Work exact | Duration error, median | Within x1.25 | Within x2 "
             "| No estimate | Median ms | p90 ms | Tokens in/out | USD |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for folder in folders:
        model = read(folder)[3]["model"]
        for level in LEVELS:
            s = score(folder, level)
            if not s["calls"]:
                continue
            n, e = s["calls"], s["estimates"]
            error = f"x{s['median_factor']:.2f}" if s["median_factor"] is not None else "-"
            ms = [f"{s[k]:.0f}" if s[k] is not None else "-" for k in ["median_ms", "p90_ms"]]
            tokens = f"{s['input_tokens']:.0f}/{s['output_tokens']:.0f}" if s["input_tokens"] is not None else "-"
            lines.append(f"| {model} | {level} | {s['valid']}/{n} | {percent(s['work_exact'], n)} | {error} "
                         f"| {percent(s['within_1_25'], e)} | {percent(s['within_2'], e)} | {s['no_estimate']} "
                         f"| {' | '.join(ms)} | {tokens} | {s['usd']:.4f} |")
    return "\n".join(lines)


def write_report(folder):
    folder = Path(folder)
    rows, references, measured, settings = read(folder)
    lines = [f"# {settings['model']} ({settings['provider']})", "",
             f"Date: {settings['date']}. Repeats: {settings['repeats']}. "
             f"Warm-up: {settings.get('warmup_ms', '-')} ms (load {settings.get('warmup_load_ms', '-')} ms), not counted.",
             f"GPU processes at start: {settings.get('gpu_processes_at_start')}. Stopped early: {settings.get('stopped', 'no')}.",
             "", table([folder]), "",
             "Invalid answers count as wrong. Duration error only uses answers with an estimate.", "",
             "## Duration error, cases with / without an explicit timer in the spec", "",
             "| Level | With timer: median, within x2 | Without timer: median, within x2 |", "|---|---|---|"]
    for level in LEVELS:
        cells = []
        for timer in [True, False]:
            s = score(folder, level, timer)
            cells.append(f"x{s['median_factor']:.2f}, {percent(s['within_2'], s['estimates'])}"
                         if s["median_factor"] is not None else "-")
        lines.append(f"| {level} | {cells[0]} | {cells[1]} |")
    lines += ["", "## Per tag precision / recall, per level", "", "| Tag | " + " | ".join(LEVELS) + " |",
              "|---|---|---|---|"]
    for tag in TAGS:
        cells = []
        for level in LEVELS:
            c = score(folder, level)["tags"][tag]
            cells.append(f"{percent(c['tp'], c['tp'] + c['fp'])} / {percent(c['tp'], c['tp'] + c['fn'])}")
        lines.append(f"| {tag} | " + " | ".join(cells) + " |")
    lines += ["", "## Answers (work types; seconds of each repeat)", "",
              "| Case | Reference | Measured | " + " | ".join(LEVELS) + " |", "|---|---|---|---|---|---|"]
    for case in sorted(measured):
        cells = []
        for level in LEVELS:
            mine = [r for r in rows if r["case"] == case and r["level"] == level]
            texts = []
            for r in mine:
                p = r["prediction"]
                texts.append("invalid" if not p else ",".join(p["work_types"]) + " " +
                             ("null" if p["duration_seconds"] is None else f"{p['duration_seconds']:g}s"))
            cells.append("; ".join(texts))
        lines.append(f"| {case} | {','.join(references[case]['work_types'])} | {measured[case]:.1f} s | " + " | ".join(cells) + " |")
    (folder / "report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        write_report(sys.argv[1])
        print((Path(sys.argv[1]) / "report.md").read_text())
    else:
        print(table(sys.argv[1:]))
