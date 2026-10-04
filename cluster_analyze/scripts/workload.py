# What does the batch workload look like: task clusters (for twins.py), job composition, dependency delays, one real job.

import matplotlib.pyplot as plt
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

from common import INSTANCES, TASKS, connect, save

con = connect()
FEATURES = ["plan_cpu", "plan_mem", "instance_num", "n_parents", "job_n_tasks"]
JOB = "j_522042"
COLORS = {"M": "tab:blue", "R": "tab:orange", "J": "tab:green"}

tasks = con.execute(f"""
    SELECT job_name, task_name, plan_cpu, plan_mem, instance_num,
           CASE WHEN starts_with(task_name, 'task_') THEN 0
                ELSE len(string_split(task_name, '_')) - 1 END AS n_parents,
           count(*) OVER (PARTITION BY job_name) AS job_n_tasks
    FROM {TASKS}
    WHERE hash(job_name) % 100 < 5 AND plan_cpu IS NOT NULL AND plan_mem IS NOT NULL AND instance_num > 0
    ORDER BY job_name, task_name
""").fetchnumpy()

X = np.column_stack([np.log1p(tasks[f].astype(float)) for f in FEATURES])
Z = (X - X.mean(axis=0)) / X.std(axis=0)

rng = np.random.default_rng(0)
fit_rows = rng.choice(len(Z), size=200_000, replace=False)
test_rows = fit_rows[:20_000]
k_values = list(range(2, 13))
silhouettes = []
for k in k_values:
    kmeans = KMeans(n_clusters=k, n_init=5, random_state=0).fit(Z[fit_rows])
    silhouettes.append(silhouette_score(Z[test_rows], kmeans.predict(Z[test_rows])))
K = k_values[np.argmax(silhouettes)]

cluster = KMeans(n_clusters=K, n_init=10, random_state=0).fit(Z[fit_rows]).predict(Z)
pq.write_table(pa.table({"job_name": tasks["job_name"], "task_name": tasks["task_name"], "cluster": cluster}),
               "data/parquet/task_clusters.parquet")

ops = con.execute(f"""
    SELECT CASE WHEN starts_with(task_name, 'task_') THEN 'independent'
                WHEN task_name = 'MergeTask' THEN 'Merge'
                ELSE regexp_extract(task_name, '^([A-Z])', 1) END AS op,
           100 * count(*) / sum(count(*)) OVER () AS tasks,
           100 * sum(instance_num) / sum(sum(instance_num)) OVER () AS instances
    FROM {TASKS}
    WHERE instance_num > 0
    GROUP BY op HAVING count(*) > 1000 ORDER BY instances DESC
""").fetchnumpy()
names = {"M": "map (M)", "R": "reduce (R)", "J": "join (J)", "independent": "independent", "Merge": "merge"}
positions = np.arange(len(ops["op"]))
plt.figure(figsize=(10, 4.5))
plt.bar(positions - 0.2, ops["tasks"], width=0.4, label="% of tasks")
plt.bar(positions + 0.2, ops["instances"], width=0.4, label="% of instances (= placement decisions)")
for i in positions:
    plt.text(i - 0.2, ops["tasks"][i] + 1, f"{ops['tasks'][i]:.0f}", ha="center")
    plt.text(i + 0.2, ops["instances"][i] + 1, f"{ops['instances'][i]:.0f}", ha="center")
plt.xticks(positions, [names[o] for o in ops["op"]])
plt.ylabel("%")
plt.title(f"Map tasks are {ops['tasks'][0]:.0f} % of tasks but {ops['instances'][0]:.0f} % of placement decisions")
plt.legend()
save("workload_job_composition")

# Delay = start of a task - end of its last parent ("J4_2_3" = task 4 waits for tasks 2 and 3).
con.execute(f"""
    CREATE TABLE nodes AS
    WITH t AS (
        SELECT job_name, task_name, start_time, end_time, instance_num,
               regexp_replace(task_name, '_Stg[0-9]+$', '') AS base
        FROM {TASKS}
        WHERE hash(job_name) % 100 < 5 AND status = 'Terminated'
          AND start_time > 0 AND end_time >= start_time AND regexp_matches(task_name, '^[A-Z][0-9]')
    )
    SELECT *, TRY_CAST(regexp_extract(base, '^[A-Z]([0-9]+)', 1) AS INT) AS node,
           list_transform(string_split(base, '_')[2:], x -> TRY_CAST(x AS INT)) AS parents
    FROM t
""")
data = con.execute("""
    WITH edges AS (
        SELECT job_name, task_name, node, unnest(parents) AS parent FROM nodes WHERE len(parents) > 0
    ),
    ready AS (
        SELECT e.job_name, e.task_name, max(p.end_time) AS ready_time
        FROM edges e JOIN nodes p ON p.job_name = e.job_name AND p.node = e.parent
        GROUP BY e.job_name, e.task_name
    )
    SELECT c.start_time - r.ready_time AS delay
    FROM ready r JOIN nodes c USING (job_name, task_name)
""").fetchnumpy()
delay = data["delay"]
print(f"{len(delay):,} tasks with parents")

shares = [100 * (delay < 0).mean(), 100 * ((delay >= 0) & (delay <= 2)).mean(), 100 * (delay > 2).mean()]
plt.figure(figsize=(8, 4))
plt.bar(["starts before its last\nparent ends (overlap)", "starts 0-2 s after", "waits more than 2 s"], shares,
        color=["tab:blue", "tab:green", "tab:red"])
for i, value in enumerate(shares):
    plt.text(i, value + 1, f"{value:.1f} %", ha="center")
plt.ylabel("% of tasks with parents")
plt.title("When does a task start, relative to its parents?")
save("workload_delay_categories")

# One real job drawn as a DAG, column = depth.
job_tasks = con.execute(f"SELECT task_name, instance_num FROM {TASKS} WHERE job_name = '{JOB}'").fetchall()
parents = {name.split("_")[0][1:]: name.split("_")[1:] for name, _ in job_tasks}
depth = {}
while len(depth) < len(parents):
    for node, ps in parents.items():
        if node not in depth and all(p in depth for p in ps):
            depth[node] = 1 + max([depth[p] for p in ps], default=-1)
position = {}
for node in sorted(parents, key=lambda n: (depth[n], n)):
    same_column = [n for n in position if depth[n] == depth[node]]
    position[node] = (depth[node] * 3, -len(same_column) * 2)

plt.figure(figsize=(10, 4))
for name, pods in job_tasks:
    node = name.split("_")[0][1:]
    x, y = position[node]
    for p in parents[node]:
        px, py = position[p]
        plt.annotate("", xy=(x - 1, y), xytext=(px + 1, py), arrowprops={"arrowstyle": "->", "color": "gray", "lw": 2})
    plt.text(x, y, f"{name.split('_')[0]}\n{pods} pod{'s' if pods > 1 else ''}", ha="center", va="center", fontsize=13,
             color="white", bbox={"boxstyle": "round,pad=0.6", "color": COLORS[name[0]]})
plt.xlim(-1.5, max(x for x, _ in position.values()) + 1.5)
plt.ylim(min(y for _, y in position.values()) - 1.5, 1.5)
plt.axis("off")
plt.title(f"A real job from the trace ({JOB}): M = map, R = reduce, J = join; arrows = dependencies")
save("workload_job_dag")

pods = con.execute(f"""
    SELECT task_name, start_time, end_time, machine_id FROM {INSTANCES}
    WHERE job_name = '{JOB}' AND start_time > 0
    ORDER BY min(start_time) OVER (PARTITION BY task_name), task_name, start_time
""").fetchnumpy()
job_start = pods["start_time"].min()
plt.figure(figsize=(10, 5))
for name in dict.fromkeys(pods["task_name"]):
    rows = np.flatnonzero(pods["task_name"] == name)
    plt.hlines(rows, pods["start_time"][rows] - job_start, pods["end_time"][rows] - job_start + 0.5,
               color=COLORS[name[0]], linewidth=2,
               label=f"{name.split('_')[0]} ({len(rows)} pod{'s' if len(rows) > 1 else ''})")
plt.gca().invert_yaxis()
plt.xlabel("seconds since the job started")
plt.ylabel("pods (one line each)")
plt.title("Its pods over time: copies start together, 3 stragglers make the next stage wait")
plt.legend(loc="upper right")
save("workload_job_timeline")
