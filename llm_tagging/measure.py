#!/usr/bin/env python3
"""Run each case in Docker with its pod limits and measure what it really does.

Duration comes from Docker's own start/finish timestamps (no pull, no creation).
CPU time, peak memory and disk bytes come from the container's cgroup, read every
0.2 s while it runs, so the last 0.2 s may be missing. Writes measurements.json.
"""

import argparse
import calendar
import json
import os
from pathlib import Path
import platform
import subprocess
import time

HERE = Path(__file__).resolve().parent
UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30}


def memory_bytes(text):
    for unit, factor in UNITS.items():
        if text.endswith(unit):
            return int(text[:-2]) * factor
    return int(text)


def cpus(text):
    return int(text[:-1]) / 1000 if text.endswith("m") else float(text)


def docker_command(pod, name):
    spec = pod["spec"]
    container = spec["containers"][0]
    limits = container["resources"]["limits"]
    user = spec["securityContext"]
    memory = str(memory_bytes(limits["memory"]))
    return ["docker", "run", "-d", "--name", name,
            "--cpus", str(cpus(limits["cpu"])), "--memory", memory, "--memory-swap", memory,
            "--user", f"{user['runAsUser']}:{user['runAsGroup']}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--entrypoint", container["command"][0], container["image"], *container["command"][1:]]


def read_cgroup(folder):
    cpu = dict(line.split() for line in (folder / "cpu.stat").read_text().splitlines())
    read = write = 0
    for line in (folder / "io.stat").read_text().splitlines():
        fields = dict(item.split("=") for item in line.split()[1:])
        read += int(fields.get("rbytes", 0))
        write += int(fields.get("wbytes", 0))
    return {"cpu_seconds": int(cpu["usage_usec"]) / 1e6,
            "memory_peak_bytes": int((folder / "memory.peak").read_text()),
            "read_bytes": read, "write_bytes": write}


def nanoseconds(stamp):
    # Docker gives 2026-10-09T20:00:00.123456789Z; keep the nanoseconds.
    whole, _, fraction = stamp.rstrip("Z").partition(".")
    seconds = calendar.timegm(time.strptime(whole, "%Y-%m-%dT%H:%M:%S"))
    return int(seconds) * 10**9 + int(fraction.ljust(9, "0")[:9])


def duration_class(seconds):
    return "short" if seconds < 10 else "medium" if seconds < 60 else "long"


def run_once(case, repeat):
    name = f"llm-tagging-{case['id']}-{repeat}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    container_id = subprocess.run(docker_command(case["pod"], name), check=True,
                                  capture_output=True, text=True).stdout.strip()
    folder = Path(f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope")
    usage = None
    while folder.exists():
        try:
            usage = read_cgroup(folder)
        except (OSError, ValueError, KeyError):
            pass  # the cgroup disappeared while being read
        time.sleep(0.2)
    subprocess.run(["docker", "wait", name], check=True, capture_output=True)
    state = json.loads(subprocess.run(["docker", "inspect", name], check=True,
                                      capture_output=True, text=True).stdout)[0]["State"]
    logs = subprocess.run(["docker", "logs", "--tail", "5", name], capture_output=True, text=True)
    subprocess.run(["docker", "rm", name], check=True, capture_output=True)
    seconds = (nanoseconds(state["FinishedAt"]) - nanoseconds(state["StartedAt"])) / 1e9
    return {"repeat": repeat, "seconds": round(seconds, 3), "class": duration_class(seconds),
            "exit_code": state["ExitCode"], "oom_killed": state["OOMKilled"], **(usage or {}),
            "last_output": (logs.stdout + logs.stderr).strip()[-500:]}


def cpu_model():
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.machine()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cases", help="Comma-separated IDs, default all")
    parser.add_argument("--out", type=Path, default=HERE / "measurements.json")
    args = parser.parse_args()
    cases = json.loads((HERE / "cases.json").read_text())
    if args.cases:
        cases = [c for c in cases if c["id"] in args.cases.split(",")]
    for image in sorted({c["pod"]["spec"]["containers"][0]["image"] for c in cases}):
        subprocess.run(["docker", "pull", "-q", image], check=True, capture_output=True)
    result = {"host": {"cpu": cpu_model(),
                       "cores": os.cpu_count(), "load_average_at_start": os.getloadavg(),
                       "date": time.strftime("%Y-%m-%d %H:%M:%S %z")},
              "cases": {}}
    # Alternate cases between repeats so a slow period of the machine hits every case.
    for repeat in range(1, args.repeats + 1):
        for case in cases:
            run = run_once(case, repeat)
            result["cases"].setdefault(case["id"], []).append(run)
            print(f"{case['id']} #{repeat}: {run['seconds']:8.2f} s  {run['class']:6}  "
                  f"cpu {run.get('cpu_seconds', 0):6.2f} s  "
                  f"mem {run.get('memory_peak_bytes', 0) / 2**20:6.1f} MiB  "
                  f"write {run.get('write_bytes', 0) / 2**20:7.1f} MiB  exit {run['exit_code']}",
                  flush=True)
            args.out.write_text(json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    main()
