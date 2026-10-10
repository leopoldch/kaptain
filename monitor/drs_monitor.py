#!/usr/bin/env python3
# DRS monitor (Jian et al. 2024, Algorithm 3), ported from the authors' drs-monitor/monitor.py
# (commit c6054c9). Standard library only, runs as a DaemonSet on every worker.
#
# Kept: a sample every 0.5 s, queues of 10, the state returned on request is the mean of the
# last AT=2 samples, and the six metrics and their units (% for CPU and memory, KB/s for the
# network card and the disk). Departures, numbered in reproduction-drs/ECARTS-DRS.md:
#   E18 CPU from /proc/stat instead of `top` (same quantity: 100 - idle, iowait counted busy)
#   E19 disk from /proc/diskstats (device level) instead of `sudo iotop` (process level)
#   E20 network rate over the last period; the code's rate was an average since start
#   E21 one sampling thread instead of four
#   E22 HTTP/JSON instead of a raw socket, and every sample logged as a JSON line

import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE = os.environ.get("NODE_NAME", "unknown")
BIND = os.environ.get("DRS_BIND", "0.0.0.0")
PORT = int(os.environ.get("DRS_PORT", "9101"))
PERIOD_S = float(os.environ.get("DRS_PERIOD_S", "0.5"))
AVGTIME = int(os.environ.get("DRS_AVGTIME", "2"))
QUEUE = int(os.environ.get("DRS_QUEUE", "10"))
# The first interface present is the state's network card; the others are only logged.
IFACES = [name for name in os.environ.get("DRS_IFACES", "wg-vps,wlan0,wlP1p1s0").split(",") if name]
# Same rule for the disk: the first device present is measured (SD card on the Pi, sda on the VMs).
DISKS = [name for name in os.environ.get("DRS_DISK", "mmcblk0,sda").split(",") if name]
METRICS = ("cpu", "mem", "recv", "tran", "read", "write")

samples = deque(maxlen=QUEUE)
lock = threading.Lock()


def cpu_times():
    # user nice system idle iowait irq softirq steal; guest is already inside user.
    fields = [int(v) for v in open("/proc/stat").readline().split()[1:9]]
    return sum(fields), fields[3]


def memory_percent():
    info = {}
    for line in open("/proc/meminfo"):
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0])
    return 100.0 * (info["MemTotal"] - info["MemAvailable"]) / info["MemTotal"]


def net_bytes():
    counters = {}
    for line in open("/proc/net/dev").readlines()[2:]:
        name, data = line.split(":", 1)
        values = data.split()
        counters[name.strip()] = (int(values[0]), int(values[8]))
    return counters


def disk_sectors():
    """Sectors read and written, per device name."""
    counters = {}
    for line in open("/proc/diskstats"):
        fields = line.split()
        counters[fields[2]] = (int(fields[5]), int(fields[9]))
    return counters


def sampler():
    total, idle = cpu_times()
    nets, disks, then = net_bytes(), disk_sectors(), time.monotonic()
    state_iface = next((name for name in IFACES if name in nets), None)
    if state_iface is None:
        raise RuntimeError(f"none of {IFACES} in /proc/net/dev")
    state_disk = next((name for name in DISKS if name in disks), None)
    if state_disk is None:
        raise RuntimeError(f"none of {DISKS} in /proc/diskstats")
    disk = disks[state_disk]
    print(json.dumps({"event": "monitor_start", "node": NODE, "iface": state_iface, "disk": state_disk,
                      "period_s": PERIOD_S, "avgtime": AVGTIME}), flush=True)

    while True:
        time.sleep(PERIOD_S)
        now_total, now_idle = cpu_times()
        now_nets, now_disk, now = net_bytes(), disk_sectors()[state_disk], time.monotonic()
        elapsed = now - then
        busy = now_total - total
        cpu = 100.0 * (busy - (now_idle - idle)) / busy if busy else 0.0

        def rate(new, old):  # bytes -> KB/s, as the code's /1024
            return (new - old) / elapsed / 1024

        ifaces = {name: [round(rate(now_nets[name][0], nets[name][0]), 3),
                         round(rate(now_nets[name][1], nets[name][1]), 3)]
                  for name in IFACES if name in now_nets and name in nets}
        sample = {
            "cpu": cpu,
            "mem": memory_percent(),
            "recv": ifaces[state_iface][0],
            "tran": ifaces[state_iface][1],
            "read": rate(512 * now_disk[0], 512 * disk[0]),
            "write": rate(512 * now_disk[1], 512 * disk[1]),
        }
        with lock:
            samples.append(sample)
        print(json.dumps({"event": "sample", "t": round(time.time(), 3), "node": NODE,
                          **{k: round(v, 3) for k, v in sample.items()}, "ifaces": ifaces}), flush=True)
        total, idle, nets, disk, then = now_total, now_idle, now_nets, now_disk, now


def state():
    with lock:
        recent = list(samples)[-AVGTIME:]
    if len(recent) < AVGTIME:
        return None
    return {m: sum(s[m] for s in recent) / len(recent) for m in METRICS}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/state":
            body = state()
            status = 200 if body is not None else 503
            body = {**(body or {"error": "warming up"}), "node": NODE, "t": time.time()}
        elif self.path == "/healthz":
            status, body = 200, {"status": "ok", "node": NODE}
        else:
            status, body = 404, {"error": "not found"}
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # one line per request would drown the samples
        pass


if __name__ == "__main__":
    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while thread.is_alive():  # a dead sampler must restart the pod, not serve stale state
        time.sleep(1)
    raise SystemExit("sampler stopped")
