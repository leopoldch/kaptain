# How precise are the durations, and how does the machine state behave?

import warnings

import matplotlib.pyplot as plt
import numpy as np
from sklearn.decomposition import NMF, PCA

from common import INSTANCES, USAGE, connect, save

warnings.filterwarnings("ignore")
con = connect()
VALID_INSTANCE = "start_time > 0 AND end_time >= start_time"

# Rounding a duration d to the second gives an error of 100/d %.
errors = [5, 10, 20, 50, 100]
shares = [con.sql(f"""
    SELECT 100.0 * count(*) FILTER (WHERE end_time - start_time <= {100 / e}) / count(*)
    FROM {INSTANCES} WHERE status = 'Terminated' AND {VALID_INSTANCE}
""").fetchone()[0] for e in errors]
plt.figure(figsize=(8, 4))
plt.bar([f">= {e} %" for e in errors], shares, color="tab:purple")
plt.xlabel("error from rounding to the second (% of duration)")
plt.ylabel("% of terminated instances")
plt.title("Share of instances whose duration is measured too coarsely")
save("explore_duration_precision")

# mem_gps and mpki are left out: they are empty in 79 % of the rows.
metrics = ["cpu_util_percent", "mem_util_percent", "net_in", "net_out", "disk_io_percent"]
sample = con.sql(f"""
    SELECT {", ".join(metrics)} FROM {USAGE} WHERE disk_io_percent BETWEEN 0 AND 100
    USING SAMPLE reservoir(200000 ROWS) REPEATABLE (42)
""").fetchnumpy()
X = np.column_stack([sample[m] for m in metrics])
X = (X - X.mean(axis=0)) / X.std(axis=0)
pca = PCA()
pca.fit(X)
explained = 100 * pca.explained_variance_ratio_
components = [f"C{i + 1}" for i in range(len(explained))]

plt.figure(figsize=(7, 4))
plt.bar(components, explained, label="per component")
plt.plot(components, np.cumsum(explained), color="red", marker="o", label="cumulative")
plt.ylabel("% of explained variance")
plt.title("PCA of machine state")
plt.legend()
save("explore_pca_variance")

load = con.sql(f"""
    SELECT time_stamp // 300 * 300 / 86400 AS day,
           approx_quantile(cpu_util_percent, 0.1) AS p10,
           approx_quantile(cpu_util_percent, 0.5) AS p50,
           approx_quantile(cpu_util_percent, 0.9) AS p90
    FROM {USAGE} GROUP BY day ORDER BY day
""").fetchnumpy()
plt.figure(figsize=(10, 4))
plt.fill_between(load["day"], load["p10"], load["p90"], alpha=0.3, label="10 % to 90 % of machines")
plt.plot(load["day"], load["p50"], color="black", linewidth=0.8, label="median machine")
plt.xlabel("trace day")
plt.ylabel("CPU used (%)")
plt.ylim(0, 100)
plt.title(f"CPU load of all machines at the same moment (median p90 - p10 gap: "
          f"{np.median(load['p90'] - load['p10']):.0f} points)")
plt.legend()
save("explore_decision_margin")

hourly = con.sql(f"""
    SELECT machine_id, time_stamp // 3600 AS hour, avg(cpu_util_percent) AS cpu
    FROM {USAGE} GROUP BY machine_id, hour
""").fetchnumpy()
machines = sorted(set(hourly["machine_id"]))
row = {m: i for i, m in enumerate(machines)}
hours = int(hourly["hour"].max()) + 1
matrix = np.full((len(machines), hours), np.nan)
for machine, hour, cpu in zip(hourly["machine_id"], hourly["hour"], hourly["cpu"]):
    matrix[row[machine], int(hour)] = cpu
matrix = matrix[np.isnan(matrix).mean(axis=1) <= 0.1]
for i in range(len(matrix)):
    matrix[i, np.isnan(matrix[i])] = np.nanmean(matrix[i])

model = NMF(n_components=4, init="nndsvd", max_iter=1000, random_state=0)
dominant = model.fit_transform(matrix).argmax(axis=1)
plt.figure(figsize=(10, 4))
for k in range(4):
    plt.plot(np.arange(hours) / 24, model.components_[k],
             label=f"profile {k + 1} ({100 * (dominant == k).mean():.0f} % of machines)")
plt.xlabel("trace day")
plt.ylabel("profile weight")
plt.title("The 4 CPU load profiles found by NMF")
plt.legend()
save("explore_nmf_profiles")
