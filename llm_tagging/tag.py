#!/usr/bin/env python3
"""Tag each case and estimate its duration from what a scheduler knows before launch.

Levels: the pod spec, + image metadata from the registry, + the candidate node.

Providers: ollama (local) or openai (paid, needs a budget).
"""

import argparse
import json
import os
from pathlib import Path
import random
import subprocess
import time
from urllib.request import Request, urlopen

from report import write_report

HERE = Path(__file__).resolve().parent
LEVELS = ["pod", "pod_image", "pod_image_node"]
TAGS = ["cpu", "memory", "disk", "network", "wait", "unknown"]


def load(path):
    return json.loads(Path(path).read_text())


def model_input(case, level, images, node):
    # Only what the scheduler has: never the case id, source, reference or measurement.
    task = {"pod": case["pod"]}
    if level != "pod":
        refs = sorted({c["image"] for c in case["pod"]["spec"]["containers"]})
        task["images"] = {ref: images[ref]["metadata"] for ref in refs}
    if level == "pod_image_node":
        task["node"] = {k: v for k, v in node.items() if not k.startswith("_")}
    return task


def validate(answer):
    if not isinstance(answer, dict) or set(answer) != {"work_types", "duration_seconds", "evidence", "missing_information"}:
        raise ValueError("expected exactly the four response fields")
    tags = answer["work_types"]
    if not tags or len(set(tags)) != len(tags) or any(t not in TAGS for t in tags):
        raise ValueError("invalid or duplicate work tags")
    if len(tags) > 1 and ("wait" in tags or "unknown" in tags):
        raise ValueError("wait and unknown must stand alone")
    seconds = answer["duration_seconds"]
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0):
        raise ValueError("duration_seconds must be a positive number or null")
    return answer


def post(url, body, timeout, api_key=None):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    with urlopen(Request(url, json.dumps(body).encode(), headers), timeout=timeout) as response:
        return json.load(response)


def request_body(provider, model, prompt, task, schema, max_tokens):
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(task, sort_keys=True)}]
    if provider == "ollama":
        return {"model": model, "messages": messages, "stream": False, "format": schema,
                "think": False, "keep_alive": "30m",
                "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": 8192}}
    return {"model": model, "input": messages, "store": False, "max_output_tokens": max_tokens,
            "reasoning": {"effort": "none"},
            "text": {"format": {"type": "json_schema", "name": "task_tags", "strict": True, "schema": schema}}}


def answer_text(provider, response):
    if provider == "ollama":
        if response.get("done_reason") == "length":
            raise ValueError("output cut at max tokens")
        return response["message"]["content"]
    if response.get("status") != "completed":
        raise ValueError("incomplete response")
    return "".join(part["text"] for item in response["output"] if item["type"] == "message"
                   for part in item["content"] if part["type"] == "output_text")


def gpu_processes():
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=process_name,used_memory",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout
        # "/path/to/program --many --flags, 152 MiB" -> "program, 152 MiB"
        return [line.split()[0].split("/")[-1].rstrip(",") + ", " + line.rsplit(",", 1)[1].strip()
                for line in out.splitlines() if line.strip()]
    except OSError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["ollama", "openai"], default="ollama")
    parser.add_argument("--model", default="qwen3:4b")
    parser.add_argument("--repeats", type=int, default=3, help="Same call again, for latency")
    parser.add_argument("--cases", help="Comma-separated IDs, default all")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-tokens", type=int, default=300)
    parser.add_argument("--out", type=Path, help="New result folder")
    parser.add_argument("--budget-usd", type=float, help="OpenAI only: stop before exceeding it")
    parser.add_argument("--input-price", type=float, help="OpenAI only: USD per million input tokens")
    parser.add_argument("--output-price", type=float, help="OpenAI only: USD per million output tokens")
    args = parser.parse_args()
    key = os.environ.get("OPENAI_API_KEY")
    if args.provider == "openai" and not (key and args.budget_usd and args.input_price and args.output_price):
        parser.error("openai needs OPENAI_API_KEY, --budget-usd, --input-price and --output-price")

    cases = load(HERE / "cases.json")
    if args.cases:
        cases = [c for c in cases if c["id"] in args.cases.split(",")]
    images = load(HERE / "images.json")
    node = load(HERE / "node.json")
    prompt = (HERE / "prompt.txt").read_text()
    schema = load(HERE / "response_schema.json")
    name = args.out or HERE / "results" / (args.model.replace(":", "-").replace("/", "-") + time.strftime("-%Y%m%d-%H%M%S"))
    out = Path(name)
    out.mkdir(parents=True)
    for file in ["cases.json", "references.json", "images.json", "node.json", "prompt.txt", "response_schema.json"]:
        (out / file).write_bytes((HERE / file).read_bytes())
    measured = HERE / "measurements.json"
    if measured.exists():
        (out / "measurements.json").write_bytes(measured.read_bytes())

    settings = {**{k: str(v) for k, v in vars(args).items() if k != "out"},
                "date": time.strftime("%Y-%m-%d %H:%M:%S %z"), "gpu_processes_at_start": gpu_processes()}
    if args.provider == "ollama":
        # Untimed warm-up: loads the model so that measured calls are all warm.
        with urlopen("http://localhost:11434/api/tags", timeout=30) as response:
            installed = json.load(response)["models"]
        settings["model_digest"] = next((m["digest"] for m in installed if m["name"] == args.model), None)
        start = time.perf_counter()
        warm = post("http://localhost:11434/api/chat", request_body(
            "ollama", args.model, prompt, model_input(cases[0], "pod", images, node), schema, args.max_tokens), args.timeout)
        settings["warmup_ms"] = round((time.perf_counter() - start) * 1000, 1)
        settings["warmup_load_ms"] = round(warm.get("load_duration", 0) / 1e6, 1)

    jobs = [(c, level, r) for c in cases for level in LEVELS for r in range(1, args.repeats + 1)]
    random.Random(7).shuffle(jobs)
    spent = 0.0
    with (out / "responses.jsonl").open("w") as file:
        for case, level, repeat in jobs:
            task = model_input(case, level, images, node)
            row = {"case": case["id"], "level": level, "repeat": repeat, "prediction": None}
            start = time.perf_counter()
            try:
                body = request_body(args.provider, args.model, prompt, task, schema, args.max_tokens)
                if args.provider == "openai":
                    count = post("https://api.openai.com/v1/responses/input_tokens",
                                 {k: body[k] for k in ["model", "input", "text"]}, args.timeout, key)
                    worst = (count["input_tokens"] * args.input_price + args.max_tokens * args.output_price) / 1e6
                    if spent + worst > args.budget_usd:
                        settings["stopped"] = "budget reached"
                        break
                    start = time.perf_counter()  # do not count the token counting call
                url = ("http://localhost:11434/api/chat" if args.provider == "ollama"
                       else "https://api.openai.com/v1/responses")
                response = post(url, body, args.timeout, key)
                row["call_ms"] = round((time.perf_counter() - start) * 1000, 1)
                if args.provider == "ollama":
                    row["input_tokens"] = response.get("prompt_eval_count")
                    row["output_tokens"] = response.get("eval_count")
                    row["load_ms"] = round(response.get("load_duration", 0) / 1e6, 1)
                else:
                    row["input_tokens"] = response["usage"]["input_tokens"]
                    row["output_tokens"] = response["usage"]["output_tokens"]
                    row["usd"] = (row["input_tokens"] * args.input_price +
                                  row["output_tokens"] * args.output_price) / 1e6
                    spent += row["usd"]
                row["raw"] = answer_text(args.provider, response)
                row["prediction"] = validate(json.loads(row["raw"]))
            except (OSError, ValueError, KeyError, TypeError) as error:
                row["error"] = f"{type(error).__name__}: {error}"[:300]
                if args.provider == "openai" and "usd" not in row:
                    # The call itself failed: its cost is unknown, so stop. An invalid answer
                    # was billed and counted above, so it is just a wrong answer.
                    settings["stopped"] = "paid call failed, stopping"
            row.setdefault("call_ms", round((time.perf_counter() - start) * 1000, 3))
            file.write(json.dumps(row) + "\n")
            file.flush()
            print(f"{case['id']} {level:9} #{repeat}: {'ok' if row['prediction'] else row['error']}", flush=True)
            if settings.get("stopped"):
                break
    settings["gpu_processes_at_end"] = gpu_processes()
    settings["spent_usd"] = spent
    (out / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    write_report(out)
    print(f"Report: {out / 'report.md'}")


if __name__ == "__main__":
    main()
