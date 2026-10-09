# Task tagging before launch

Question: from what a scheduler knows before starting a pod, can a model tell
what kind of work it does and how long it lasts? How well, how fast, at what cost?

Python standard library only. No Kubernetes connection. Protocol:
[../llm-tagging-protocol.md](../llm-tagging-protocol.md).

## What the model receives

Exactly what the scheduler has, nothing written by hand:

| Level | Input |
|---|---|
| `pod` | The pod spec (`cases.json`), without name, labels or schedulerName |
| `pod_image` | The same, plus each image's config read from its registry (`images.json`) |
| `pod_image_node` | The same, plus the candidate node as a Node object (`node.json`) |

Image metadata is what a registry gives without pulling layers: architecture,
compressed size, layer count, entrypoint, cmd, env, exposed ports, user and
build history. `images.py` reads it and records how long that took.

The scheduler scores each candidate node one by one, so it knows the node it is
evaluating. `node.json` describes the machine where the measurements were made:
capacity, architecture, and two non-standard labels (CPU model, disk type) of the
kind an admin or node-feature-discovery adds. In a cluster this means one call per
candidate node, unless the node does not change the answer.

## What the model returns

- `work_types`: one or more of `cpu`, `memory`, `disk`, `network`, or `wait` alone, or `unknown` alone.
- `duration_seconds`: an estimate in seconds, or `null` with no basis at all.
- `evidence`, `missing_information`: up to three short sentences each.

## What answers are compared to

- `references.json`: the work types deducible from the pod spec, and whether the
  spec states the duration (`timer`). **Draft, to review.**
- `measurements.json`: what each case really did in Docker with its pod limits,
  3 runs each: duration, CPU seconds, peak memory, bytes read/written.

Duration error is a factor: `max(predicted / measured, measured / predicted)`.
x1 is perfect, x2 is twice too long or too short. Reported: median factor, share
within x1.25 and x2, and separately for cases with and without a timer in the spec.
There are no duration classes: any cut-off (10 s, 60 s...) would be arbitrary.

## Cases

| Case | Pod | From |
|---|---|---|
| c01 | `stress-ng --cpu 1 --cpu-ops 6000` | PR #28 cpu.yaml |
| c02 | `stress-ng --vm 1 --vm-bytes 128M --vm-ops 2000000` | PR #28 memory.yaml |
| c03 | `stress-ng --hdd 1 --hdd-bytes 128M --hdd-ops 20000` | PR #28 disk.yaml |
| c04 | nginx, then `sleep 60`, then quit | PR #28 web.yaml |
| c05 | `sleep 60` | PR #28 noop.yaml |
| c06, c07, c08 | `sleep 2`, `sleep 20`, `sleep 90` | noop.yaml |
| c09 | stress-ng CPU + memory, `--timeout 20s` | cpu.yaml |
| c10 | stress-ng CPU + disk, `--timeout 20s` | cpu.yaml |
| c11 | nginx for 20 s | web.yaml |
| c12 | 256 MiB of random data, then gzip | busybox |

The commands make most answers obvious: these 12 cases check that a model reads
explicit commands and follows the definitions. They do not show its value on
opaque, real tasks. No hand-written rules are used: they would amount to
hard-coding a scheduler for known programs.

## Run

```bash
python3 images.py                         # registry metadata, ~0.4 s per image
python3 measure.py --repeats 3            # ~25 min, nothing else running
python3 tag.py --model qwen3:4b           # 12 cases x 3 levels x 3 repeats
python3 report.py results/A results/B     # one comparison table
python3 plots.py results/A results/B      # PNG charts in results/figures (needs matplotlib)
python3 -m unittest test_tagging
```

`tag.py` sends one untimed warm-up call first, so every measured call has the
model loaded. Repeats are the same call again. With Ollama (temperature 0) they
only measure latency; OpenAI sets no temperature, so its answers can vary. It records the GPU processes at start and end, because
another program on the GPU slows the calls.

OpenAI (paid, stops before going over the budget):

```bash
export OPENAI_API_KEY=...
python3 tag.py --provider openai --model gpt-5.4-mini --budget-usd 1 --input-price 0.75 --output-price 4.50
```

## Limits

- 12 cases is a pilot, not proof of generalisation.
- Measurements are from one machine (Ryzen 7 2700X), Docker instead of Kubernetes.
- The cgroup is read every 0.2 s, so the last 0.2 s of a run may be missing.
- `results/v0-old-fixtures/`: first version (wrong memory limits for c02/c03,
  hand-written inputs). `results/v1-classes/`: duration as short/medium/long.
  Neither is comparable with the current results.
