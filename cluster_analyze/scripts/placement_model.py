# Can machine state at placement time tell that a twin will be slow (random split by job, then split by time)?

import json

import matplotlib.pyplot as plt
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, roc_curve
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from common import HOLE_END, HOLE_START, TASKS, TRACE_END, TWINS, USAGE, connect, save

con = connect()
con.execute(f"""
    CREATE TABLE slots AS
    SELECT machine_id, time_stamp // 300 AS slot, avg(cpu_util_percent) AS cpu, avg(mem_util_percent) AS mem,
           avg(net_in) AS net_in, avg(net_out) AS net_out, avg(disk_io_percent) AS disk
    FROM {USAGE} WHERE disk_io_percent BETWEEN 0 AND 100
    GROUP BY machine_id, slot
""")
con.execute("""
    CREATE TABLE medians AS
    SELECT slot, median(cpu) AS cpu, median(mem) AS mem, median(net_in) AS net_in,
           median(net_out) AS net_out, median(disk) AS disk
    FROM slots GROUP BY slot
""")
con.execute(f"""
    CREATE TABLE twins AS
    SELECT t.*, (t.duration >= 1.5 * t.task_median AND t.duration - t.task_median >= 2)::INT AS slow,
           t.cpu - m.cpu AS cpu_rel, t.mem - m.mem AS mem_rel, t.net_in - m.net_in AS net_in_rel,
           t.net_out - m.net_out AS net_out_rel, t.disk - m.disk AS disk_rel,
           l.cpu AS cpu_later, l.mem AS mem_later, l.net_in AS net_in_later,
           l.net_out AS net_out_later, l.disk AS disk_later
    FROM (SELECT *, median(duration) OVER (PARTITION BY job_name, task_name) AS task_median FROM {TWINS}
          WHERE start_time + 3600 < {TRACE_END} AND start_time + 3600 NOT BETWEEN {HOLE_START} AND {HOLE_END}) t
    JOIN slots l ON l.machine_id = t.machine_id AND l.slot = (t.start_time + 3600) // 300
    JOIN medians m ON m.slot = t.start_time // 300
""")
machine = ["cpu", "mem", "net_in", "net_out", "disk"]
relative = [m + "_rel" for m in machine]
placebo = [m + "_later" for m in machine]

data = con.execute(f"SELECT slow, {', '.join(machine + relative + placebo)}, hash(job_name || 'test') % 5 = 0 AS is_test FROM twins ORDER BY job_name, task_name, machine_id, start_time").fetchnumpy()
rng = np.random.default_rng(0)
train = rng.permutation(np.flatnonzero(~data["is_test"]))[:1_000_000]
test = rng.permutation(np.flatnonzero(data["is_test"]))[:300_000]
y_train, y_test = data["slow"][train], data["slow"][test]


def columns(names, rows):
    return np.column_stack([data[n][rows].astype(float) for n in names])


models = {
    "logistic regression": (make_pipeline(StandardScaler(), LogisticRegression()), machine),
    "gradient boosting": (HistGradientBoostingClassifier(random_state=0), machine),
    "gradient boosting, relative state": (HistGradientBoostingClassifier(random_state=0), relative),
    "placebo (state 1 h later)": (HistGradientBoostingClassifier(random_state=0), placebo),
}
scores = {}
plt.figure(figsize=(6, 6))
for name, (model, names) in models.items():
    model.fit(columns(names, train), y_train)
    scores[name] = model.predict_proba(columns(names, test))[:, 1]
    fpr, tpr, _ = roc_curve(y_test, scores[name])
    plt.plot(fpr, tpr, label=f"{name} (AUC {roc_auc_score(y_test, scores[name]):.3f})")
plt.plot([0, 1], [0, 1], color="black", linestyle="--", label="random (AUC 0.5)")
plt.xlabel("false positive rate")
plt.ylabel("true positive rate")
plt.title("Detecting a bad decision from machine state")
plt.legend(fontsize=8, loc="lower right")
save("placement_roc")

with open("results/placement.json", "w") as f:
    json.dump({"auc_model": roc_auc_score(y_test, scores["gradient boosting"]),
               "auc_placebo": roc_auc_score(y_test, scores["placebo (state 1 h later)"])}, f, indent=2)

data = con.execute(f"""
    SELECT t.start_time, t.log_d, t.cluster, t.slow, {", ".join(machine + relative + placebo)},
           b.plan_cpu, b.plan_mem, b.instance_num, coalesce(TRY_CAST(b.task_type AS INT), -1) AS task_type,
           CASE WHEN starts_with(t.task_name, 'task_') THEN 0 ELSE len(string_split(t.task_name, '_')) - 1 END AS n_parents,
           (t.start_time % 86400) // 3600 AS hour
    FROM twins t JOIN {TASKS} b USING (job_name, task_name)
    ORDER BY t.job_name, t.task_name, t.machine_id, t.start_time
""").fetchnumpy()
rng = np.random.default_rng(0)
train = rng.permutation(np.flatnonzero(data["start_time"] < 5 * 86400))[:1_000_000]
test = rng.permutation(np.flatnonzero(data["start_time"] >= 5.5 * 86400))[:300_000]

task = ["plan_cpu", "plan_mem", "instance_num", "task_type", "n_parents", "hour", "cluster"]
feature_sets = {
    "task only": task,
    "machine only": machine,
    "task + machine": task + machine,
    "task + machine\n(relative to others)": task + relative,
    "task + placebo\n(machine 1 h later)": task + placebo,
}
aucs, r2s = [], []
for names in feature_sets.values():
    classifier = HistGradientBoostingClassifier(random_state=0).fit(columns(names, train), data["slow"][train])
    aucs.append(roc_auc_score(data["slow"][test], classifier.predict_proba(columns(names, test))[:, 1]))
    regressor = HistGradientBoostingRegressor(random_state=0).fit(columns(names, train), data["log_d"][train])
    r2s.append(regressor.score(columns(names, test), data["log_d"][test]))

fig, (left, right) = plt.subplots(1, 2, figsize=(14, 4.5))
colors = ["tab:blue", "tab:orange", "tab:purple", "tab:red", "tab:green"]
left.bar(list(feature_sets), aucs, color=colors)
left.axhline(0.5, color="black", linestyle="--", label="random")
left.set_ylim(0.45, 1)
left.set_ylabel("AUC on later days")
left.set_title("Detecting a slow twin")
left.legend()
right.bar(list(feature_sets), r2s, color=colors)
right.set_ylim(0, 1)
right.set_ylabel("R² on later days")
right.set_title("Predicting the duration of a twin")
for axis, values in [(left, aucs), (right, r2s)]:
    axis.tick_params(axis="x", labelsize=8)
    for i, value in enumerate(values):
        axis.text(i, value + 0.01, f"{value:.3f}", ha="center")
save("placement_temporal_validation")
