# How much do pods over-reserve, what would relaunching stragglers save, and can real usage be predicted?

import matplotlib.pyplot as plt
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

from common import INSTANCES, TASKS, connect, save

BEFORE_LAUNCH = ["log_cpu", "log_mem", "log_instances", "log_max_instances",
                 "log_tasks", "mean_parents", "root_share", "parent_share"]
TARGETS = {"log_mem_used_ratio": "memory used / requested",
           "log_cpu_used_ratio": "CPU used / requested",
           "log_duration": "duration"}


def r2(y, prediction):
    return 1 - ((y - prediction) ** 2).mean() / ((y - y.mean()) ** 2).mean()


con = connect()
cpu_mean, cpu_peak, mem_peak = con.execute(f"""
    SELECT median(i.cpu_avg / t.plan_cpu), median(i.cpu_max / t.plan_cpu), median(i.mem_max / t.plan_mem)
    FROM {INSTANCES} i JOIN {TASKS} t USING (job_name, task_name)
    WHERE hash(job_name) % 100 < 1 AND i.status = 'Terminated' AND t.plan_cpu > 0 AND t.plan_mem > 0
""").fetchone()
labels = ["CPU\nrequested", "CPU really used\n(average)", "CPU really used\n(peak)",
          "memory\nrequested", "memory really used\n(peak)"]
values = [100, 100 * cpu_mean, 100 * cpu_peak, 100, 100 * mem_peak]
plt.figure(figsize=(10, 5))
plt.bar(labels, values, color=["lightgray", "tab:blue", "tab:blue", "lightgray", "tab:green"])
for i, value in enumerate(values):
    plt.text(i, value + 2, f"{value:.0f}", ha="center", fontsize=14)
plt.ylabel("typical pod (median), requested = 100")
plt.title(f"A typical pod uses about {100 * mem_peak:.0f} % of the memory it requests "
          f"(about {1 / mem_peak:.0f} times too much reserved)")
save("prediction_reservation_simple")

con.execute(f"""
    CREATE TABLE inst AS
    SELECT i.job_name, i.task_name, i.start_time, i.end_time, i.end_time - i.start_time AS duration,
           count(*) OVER (PARTITION BY i.job_name, i.task_name) AS n,
           row_number() OVER (PARTITION BY i.job_name, i.task_name ORDER BY i.end_time) AS finish_rank
    FROM {INSTANCES} i JOIN {TASKS} t USING (job_name, task_name)
    WHERE hash(job_name) % 100 < 1 AND i.status = 'Terminated'
      AND i.start_time > 0 AND i.end_time >= i.start_time AND t.plan_cpu > 0 AND t.plan_mem > 0
""")

# When half of the twins are done, m = median duration of the finished ones.
# A twin still running after 1.5 x m is a straggler: a copy is launched, assumed to take m.
tasks = con.execute("""
    WITH half AS (
        SELECT job_name, task_name, min(start_time) AS task_start, max(end_time) AS task_end, any_value(n) AS n,
               max(end_time) FILTER (WHERE finish_rank <= n / 2) AS t_half,
               greatest(median(duration) FILTER (WHERE finish_rank <= n / 2), 1) AS m
        FROM inst WHERE n >= 10 GROUP BY job_name, task_name
    ),
    sim AS (
        SELECT i.end_time, greatest(h.t_half, i.start_time + 1.5 * h.m) AS detect, h.*
        FROM inst i JOIN half h USING (job_name, task_name)
    )
    SELECT any_value(task_end - task_start) AS before, any_value(n) AS n,
           max(CASE WHEN end_time > detect THEN least(end_time, detect + m) ELSE end_time END)
               - any_value(task_start) AS after,
           count(*) FILTER (WHERE end_time > detect) AS copies
    FROM sim GROUP BY job_name, task_name
""").fetchnumpy()
gain = 100 * (tasks["before"] - tasks["after"]) / np.maximum(tasks["before"], 1)
copies = 100 * tasks["copies"].sum() / tasks["n"].sum()
plt.figure(figsize=(9, 4))
plt.hist(gain, bins=np.arange(0, 101, 2))
plt.yscale("log")
plt.xlabel("task completion time saved (%)")
plt.ylabel("number of tasks (log scale)")
plt.title(f"Relaunching stragglers ({len(gain):,} tasks with 10+ twins)\n"
          f"faster: {100 * (gain > 0).mean():.0f} % of tasks, mean saving {gain.mean():.1f} %, "
          f"cost: {copies:.1f} copies per 100 instances")
save("prediction_straggler_gain")

# What a job will really consume, from before-launch information only. Temporal split:
# train on days 0-5, test on days 5.5-7.75. history_* = mean target of training jobs with
# exactly the same profile (a recurring job), NULL if the profile was never seen in training.
TRAIN = "submit_time < 5 * 86400"
TEST = "submit_time >= 5.5 * 86400 AND submit_time < 7.75 * 86400"
jobs = con.execute(f"""
    WITH submit AS (
        SELECT job_name, min(start_time) AS submit_time FROM {TASKS}
        WHERE hash(job_name) % 100 < 5 AND start_time > 0 GROUP BY job_name
    ),
    j AS (
        SELECT f.*, s.submit_time, (s.submit_time % 86400) // 3600 AS hour,
               hash({", ".join(f"round(f.{c}, 3)" for c in BEFORE_LAUNCH)}) AS profile
        FROM 'data/parquet/job_features.parquet' f JOIN submit s USING (job_name)
    ),
    history AS (
        SELECT profile, {", ".join(f"avg({t}) AS history_{t}" for t in TARGETS)}
        FROM j WHERE {TRAIN} GROUP BY profile
    )
    SELECT j.*, h.profile IS NOT NULL AS seen_profile, {TRAIN} AS is_train, {TEST} AS is_test,
           {", ".join(f"coalesce(h.history_{t}, 0) AS history_{t}" for t in TARGETS)}
    FROM j LEFT JOIN history h USING (profile)
    ORDER BY job_name
""").fetchnumpy()
train, test = jobs["is_train"], jobs["is_test"]
seen = test & jobs["seen_profile"]
unseen = test & ~jobs["seen_profile"]
X = np.column_stack([jobs[f] for f in BEFORE_LAUNCH + ["hour"]])

methods = ["mean of training jobs", "same profile in training\n(recurring-job history)",
           "gradient boosting\n(before-launch features)"]
subsets = {"all test jobs": test, "seen profiles": seen, "unseen profiles": unseen}
colors = ["tab:gray", "tab:green", "tab:blue"]
fig, axes = plt.subplots(1, 3, figsize=(17, 4.5), sharey=True)
for axis, target in zip(axes, TARGETS):
    y = jobs[target]
    model = HistGradientBoostingRegressor(random_state=0).fit(X[train], y[train])
    predictions = [np.full(len(y), y[train].mean()),
                   np.where(jobs["seen_profile"], jobs[f"history_{target}"], y[train].mean()),
                   model.predict(X)]
    positions = np.arange(len(subsets))
    for m, prediction in enumerate(predictions):
        values = [r2(y[subset], prediction[subset]) for subset in subsets.values()]
        axis.bar(positions + (m - 1) * 0.27, values, width=0.27, color=colors[m], label=methods[m])
        for i, value in enumerate(values):
            axis.text(i + (m - 1) * 0.27, max(value, 0) + 0.01, f"{value:.2f}", ha="center", fontsize=7)
    axis.set_xticks(positions, [f"{name}\n({subset.sum():,} jobs)" for name, subset in subsets.items()])
    axis.axhline(0, color="black", linewidth=0.8)
    axis.set_ylim(-0.2, 1)
    axis.set_title(f"Predicting {TARGETS[target]}")
axes[0].set_ylabel("R² on later days (1 = perfect)")
axes[0].legend(fontsize=8, loc="upper left")
fig.suptitle("What a job will really consume, predicted before launch (train days 0-5, test days 5.5-7.75)")
save("prediction_usage")
