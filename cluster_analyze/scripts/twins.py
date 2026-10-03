# Twins = instances of the same task on different machines: does the machine change the duration?

import json

import matplotlib.pyplot as plt
import numpy as np

from common import HOLE_END, HOLE_START, INSTANCES, TRACE_END, TWINS, USAGE, connect, save, to_percent

con = connect()
METRICS = ["cpu", "mem", "net_in", "net_out", "disk"]

# 1 % of jobs, machine state measured at most 30 s before the start, tasks with 10+ twins on 5+ machines.
con.execute(f"""
    CREATE TABLE obs AS
    SELECT job_name, task_name, i.machine_id, i.start_time, i.end_time - i.start_time AS duration,
           ln(1 + i.end_time - i.start_time) AS log_d, c.cluster,
           u.cpu_util_percent AS cpu, u.mem_util_percent AS mem, u.net_in, u.net_out, u.disk_io_percent AS disk
    FROM {INSTANCES} i
    ASOF JOIN {USAGE} u ON i.machine_id = u.machine_id AND i.start_time >= u.time_stamp
    JOIN 'data/parquet/task_clusters.parquet' c USING (job_name, task_name)
    WHERE hash(job_name) % 100 < 1 AND i.status = 'Terminated'
      AND i.start_time > 0 AND i.end_time >= i.start_time
      AND i.start_time < {TRACE_END} AND i.start_time NOT BETWEEN {HOLE_START} AND {HOLE_END}
      AND i.start_time - u.time_stamp <= 30 AND u.disk_io_percent BETWEEN 0 AND 100
""")
con.execute(f"""
    COPY (
        SELECT *,
               log_d - avg(log_d) OVER (PARTITION BY job_name, task_name) AS dev,
               log_d - avg(log_d) OVER (PARTITION BY cluster) AS dev_cluster
        FROM obs
        JOIN (SELECT job_name, task_name FROM obs GROUP BY job_name, task_name
              HAVING count(*) >= 10 AND count(DISTINCT machine_id) >= 5) USING (job_name, task_name)
    ) TO {TWINS}
""")
con.execute("DROP TABLE obs")
con.execute(f"""
    CREATE TABLE twins AS
    SELECT *, least(floor(cpu / 10) * 10, 60) AS cpu_bin,
           stddev_pop(log_d) OVER (PARTITION BY job_name, task_name) AS task_sd
    FROM {TWINS}
""")
data = con.execute("SELECT log_d, dev, cpu, mem, net_in, net_out, disk FROM twins").fetchnumpy()
print(f"{len(data['dev']):,} twins")

total = data["log_d"].var()
by_task = 100 * (1 - data["dev"].var() / total)
machine_within = con.execute("""
    SELECT sum(n * v) / sum(n) FROM (SELECT count(*) AS n, var_pop(dev) AS v FROM twins GROUP BY machine_id)
""").fetchone()[0]
by_machine = (data["dev"].var() - machine_within) / total * 100
X = np.column_stack([data[m] for m in METRICS] + [np.ones(len(data["dev"]))])
residual = data["dev"] - X @ np.linalg.lstsq(X, data["dev"], rcond=None)[0]
by_state = (data["dev"].var() - residual.var()) / total * 100

values = [by_task, by_machine + by_state, 100 - by_task - by_machine - by_state]
labels = ["what the pod is\n(its task)", "where it runs\n(node identity + state)", "unexplained\n(input size, rounding)"]
plt.figure(figsize=(10, 4.5))
plt.barh(labels[::-1], values[::-1], color=["tab:gray", "tab:orange", "tab:blue"])
for i, value in enumerate(values[::-1]):
    plt.text(value + 1, i, f"{value:.1f} %" if value >= 1 else f"{value:.2f} %", va="center", fontsize=12)
plt.xlim(0, 110)
plt.xlabel("% of the variance of log duration (11.5 M twin instances)")
plt.title("Where the information is: the task, not the node")
save("twins_information_budget")

bins = con.execute("""
    SELECT cpu_bin,
           avg(log_d) - (SELECT avg(log_d) FROM twins) AS mixed,
           avg(dev_cluster) AS same_cluster,
           avg(dev) AS same_task,
           quantile_cont(dev, 0.5) AS p50, quantile_cont(dev, 0.9) AS p90, quantile_cont(dev, 0.99) AS p99,
           quantile_cont(dev / task_sd, 0.99) FILTER (WHERE task_sd > 0) AS z99
    FROM twins GROUP BY cpu_bin ORDER BY cpu_bin
""").fetchnumpy()
labels = [f"{b:.0f}-{b + 10:.0f}" for b in bins["cpu_bin"][:-1]] + ["60+"]

plt.figure(figsize=(9, 5))
plt.plot(labels, to_percent(bins["mixed"]), marker="o", label="all tasks mixed")
plt.plot(labels, to_percent(bins["same_cluster"]), marker="o", label="same cluster")
plt.plot(labels, to_percent(bins["same_task"]), marker="o", linewidth=3, label="same task (twins)")
plt.axhline(0, color="black", linewidth=0.8)
plt.xlabel("machine CPU when the instance starts (%)")
plt.ylabel("duration difference from the mean (%)")
plt.title("The load effect depends on what is compared")
plt.legend()
save("twins_load_effect")

plt.figure(figsize=(9, 5))
for column, name in [("p50", "median"), ("p90", "P90 (1 twin in 10)"), ("p99", "P99 (1 in 100)")]:
    plt.plot(labels, to_percent(bins[column]), marker="o", label=name)
plt.axhline(0, color="black", linewidth=0.8)
plt.xlabel("machine CPU at start (%)")
plt.ylabel("duration difference from the task mean (%)")
plt.title("Does load hit the worst cases?\n"
          f"P99 divided by task spread goes from {bins['z99'][0]:.1f} to {bins['z99'][-1]:.1f} std")
plt.legend()
save("twins_quantiles")

with open("results/twins.json", "w") as f:
    json.dump({"variance_task": by_task, "variance_machine": by_machine + by_state,
               "effect_60_mixed": to_percent(bins["mixed"][-1]),
               "effect_60_twins": to_percent(bins["same_task"][-1])}, f, indent=2)
