# How close can the trace get to "this placement was bad", and do online neighbours explain it?

import matplotlib.pyplot as plt
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from common import TWINS, connect, save, to_percent

con = connect()
con.execute(f"""
    CREATE TABLE pods AS
    SELECT job_name, task_name, machine_id, start_time, duration, cpu, dev,
           start_time // 300 AS slot,
           count(*) OVER g AS group_size,
           median(duration) OVER g AS group_median,
           median(cpu) OVER g AS group_cpu,
           quantile_cont(cpu, 0.33) OVER g AS cpu_low,
           quantile_cont(cpu, 0.67) OVER g AS cpu_high
    FROM {TWINS}
    WINDOW g AS (PARTITION BY job_name, task_name, start_time // 300)
""")
con.execute("DELETE FROM pods WHERE group_size < 6")
con.execute("ALTER TABLE pods ADD COLUMN bad BOOLEAN")
con.execute("UPDATE pods SET bad = duration >= 1.5 * group_median AND duration - group_median >= 2")
n, bad_share = con.execute("SELECT count(*), avg(bad::INT) FROM pods").fetchone()
print(f"{n:,} pods in groups of 6+ twins started in the same 5 minutes; bad outcome: {100 * bad_share:.1f} %")

group = con.execute("""
    SELECT job_name, task_name, slot FROM pods
    WHERE group_size BETWEEN 10 AND 16 AND group_median BETWEEN 5 AND 30
    GROUP BY job_name, task_name, slot HAVING sum(bad::INT) = 1 AND max(duration) >= 2.5 * any_value(group_median)
    ORDER BY hash(job_name, task_name) LIMIT 1
""").fetchone()
example = con.execute(f"""
    SELECT duration, cpu, bad, group_median FROM pods
    WHERE job_name = '{group[0]}' AND task_name = '{group[1]}' AND slot = {group[2]} ORDER BY cpu
""").fetchnumpy()
median = example["group_median"][0]
plt.figure(figsize=(11, 5))
plt.bar(range(len(example["duration"])), example["duration"],
        color=["tab:red" if b else "tab:blue" for b in example["bad"]])
plt.axhline(median, color="black", label=f"median of the group = {median:.0f} s")
plt.axhline(1.5 * median, color="tab:red", linestyle="--", label="1.5 x median: above it, the pod is 'slow'")
plt.xticks(range(len(example["cpu"])), [f"{c:.0f} %" for c in example["cpu"]])
plt.xlabel("CPU of the machine each twin was placed on (sorted)")
plt.ylabel("duration (s)")
plt.title(f"One real group: {len(example['duration'])} copies of the same task, started in the same 5 minutes")
plt.legend()
save("bad_twin_group_example")

regret = con.execute("""
    WITH calm AS (
        SELECT job_name, task_name, slot, median(duration) AS calm_duration, avg(cpu) AS calm_cpu, count(*) AS n_calm
        FROM pods WHERE cpu <= cpu_low GROUP BY ALL
    )
    SELECT p.duration - c.calm_duration AS seconds, p.duration, p.cpu - c.calm_cpu AS cpu_gap
    FROM pods p JOIN calm c USING (job_name, task_name, slot)
    WHERE p.cpu >= p.cpu_high AND p.cpu - c.calm_cpu >= 20 AND c.n_calm >= 3
""").fetchnumpy()
seconds = regret["seconds"]
lost = 100 * seconds.sum() / regret["duration"].sum()
shares = [100 * (seconds > 0).mean(), 100 * (seconds == 0).mean(), 100 * (seconds < 0).mean()]
plt.figure(figsize=(9, 5))
plt.bar(["slower than\nits calm twins", "same\nduration", "faster than\nits calm twins"], shares,
        color=["tab:red", "tab:gray", "tab:green"])
for i, value in enumerate(shares):
    plt.text(i, value + 1, f"{value:.0f} %", ha="center", fontsize=14)
plt.ylabel(f"% of {len(seconds):,} pods placed on a loaded machine")
plt.title(f"If the machine did not matter, red = green. Here: {shares[0]:.0f} % vs {shares[2]:.0f} %\n"
          f"net time lost on the loaded machine: {lost:.1f} % (CPU gap {regret['cpu_gap'].mean():.0f} points)")
save("bad_regret")

reputation = con.execute("""
    SELECT machine_id,
           avg(dev) FILTER (WHERE start_time < 4 * 86400) AS first_half,
           avg(dev) FILTER (WHERE start_time >= 4 * 86400) AS second_half,
           count(*) FILTER (WHERE start_time < 4 * 86400) AS n1,
           count(*) FILTER (WHERE start_time >= 4 * 86400) AS n2
    FROM pods GROUP BY machine_id HAVING n1 >= 200 AND n2 >= 200
""").fetchnumpy()
correlation = np.corrcoef(reputation["first_half"], reputation["second_half"])[0, 1]
worst = reputation["first_half"] >= np.quantile(reputation["first_half"], 0.95)
first, second = to_percent(reputation["first_half"]), to_percent(reputation["second_half"])
plt.figure(figsize=(8, 7))
plt.plot([-15, 100], [-15, 100], color="black", linestyle=":", label="as slow on both periods")
plt.scatter(first, second, s=5, alpha=0.4, color="gray", label="one point = one machine")
plt.scatter(first[worst], second[worst], s=10, color="tab:red", label="5 % slowest on days 0-4")
plt.xlabel("days 0-4: its pods are slower than their twins by ... %")
plt.ylabel("days 4-8: same machine, slower by ... %")
plt.title(f"Slow machines stay slow (correlation {correlation:.2f})\n"
          f"the 5 % slowest of days 0-4 are still {np.mean(second[worst]):+.0f} % slower on days 4-8")
plt.legend()
save("bad_machine_reputation")

data = con.execute("""
    WITH rep AS (
        SELECT machine_id, avg(dev) AS reputation FROM pods
        WHERE start_time < 4 * 86400 GROUP BY machine_id HAVING count(*) >= 200
    )
    SELECT p.bad::INT AS bad, p.cpu - p.group_cpu AS cpu_gap, coalesce(r.reputation, 0) AS reputation
    FROM pods p LEFT JOIN rep r USING (machine_id)
    WHERE p.start_time >= 4 * 86400
    ORDER BY p.job_name, p.task_name, p.machine_id, p.start_time
""").fetchnumpy()
y = data["bad"]
scores = {"machine more loaded\nthan its twins'": data["cpu_gap"], "machine known as slow\n(days 0-4)": data["reputation"]}
both = np.column_stack([data["cpu_gap"], data["reputation"]])
half = np.random.default_rng(0).random(len(y)) < 0.5
scores["both"] = LogisticRegression().fit(both[half], y[half]).decision_function(both)
hits = [100 * y[~half].mean()] + [100 * y[~half][s[~half] >= np.quantile(s[~half], 0.9)].mean() for s in scores.values()]
aucs = [roc_auc_score(y[~half], s[~half]) for s in scores.values()]
plt.figure(figsize=(11, 5))
plt.bar(["random pick"] + [f"{n}\n(AUC {a:.2f})" for n, a in zip(scores, aucs)], hits,
        color=["tab:gray", "tab:orange", "tab:orange", "tab:orange"])
for i, value in enumerate(hits):
    plt.text(i, value + 0.3, f"{value:.0f} / 100", ha="center", fontsize=13)
plt.ylabel("really slow pods among 100 flagged")
plt.title("Flag the 10 % most suspicious pods of days 4-8: how many were really slow?")
save("bad_detection")

con.execute("""
    CREATE TABLE neighbours AS
    SELECT machine_id, time_stamp // 300 AS slot, avg(cpi) AS cpi
    FROM 'data/parquet/container_usage_10pct.parquet'
    GROUP BY machine_id, slot
""")
con.execute("""
    CREATE TABLE reserved AS
    SELECT machine_id, sum(cpu_request) / 100 AS reserved_cores
    FROM (SELECT container_id, arg_max(machine_id, time_stamp) AS machine_id, arg_max(cpu_request, time_stamp) AS cpu_request
          FROM 'data/parquet/container_meta.parquet' GROUP BY container_id)
    GROUP BY machine_id
""")
con.execute("DELETE FROM pods WHERE machine_id NOT IN (SELECT machine_id FROM neighbours)")

machines = con.execute("""
    SELECT p.machine_id, avg(p.dev) AS reputation, any_value(r.reserved_cores) AS reserved,
           (SELECT avg(cpi) FROM neighbours n WHERE n.machine_id = p.machine_id) AS cpi
    FROM pods p LEFT JOIN reserved r USING (machine_id)
    WHERE p.start_time < 4 * 86400
    GROUP BY p.machine_id HAVING count(*) >= 200
""").fetchnumpy()
reserved, cpi, reputation = machines["reserved"].astype(float), machines["cpi"].astype(float), machines["reputation"]
ok, has_cpi = ~np.isnan(reserved), ~np.isnan(cpi)
plt.figure(figsize=(9, 6))
plt.scatter(reserved[ok], to_percent(reputation[ok]), s=10, alpha=0.6)
plt.axhline(0, color="black", linewidth=0.8)
plt.xlabel("CPU cores reserved by online services on the machine")
plt.ylabel("its batch pods are slower than their twins by ... %")
plt.title(f"Slow machines host heavy online services (correlation {np.corrcoef(reserved[ok], reputation[ok])[0, 1]:.2f}, "
          f"{ok.sum()} machines)\none point = one machine; CPI of the neighbours: correlation only "
          f"{np.corrcoef(cpi[has_cpi], reputation[has_cpi])[0, 1]:.2f}")
save("bad_reputation_vs_neighbours")
