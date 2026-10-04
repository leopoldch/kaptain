# Which families of jobs exist, and can a job's family be recognized before launch?

import matplotlib.pyplot as plt
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.cluster import HDBSCAN, KMeans
from sklearn.decomposition import PCA
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.manifold import TSNE
from sklearn.metrics import adjusted_rand_score, silhouette_score
from sklearn.mixture import GaussianMixture

from common import INSTANCES, TASKS, connect, save

BLOCKS = {
    "requested": ["log_cpu", "log_mem", "log_instances", "log_max_instances"],
    "behavior": ["log_cpu_used_ratio", "log_mem_used_ratio", "log_duration", "duration_spread",
                 "straggler_share", "log_span", "failed_share"],
    "structure": ["log_tasks", "mean_parents", "root_share", "parent_share"],
}
FEATURES = [f for block in BLOCKS.values() for f in block]
# Names read from the cluster profiles (cluster 0 = largest).
FAMILIES = ["small MapReduce", "one-shot", "large DAG", "long single-stage", "micro independent", "straggler-prone"]

con = connect()
con.execute(f"""
    CREATE TABLE tasks AS
    SELECT job_name, task_name, plan_cpu, plan_mem, instance_num, task_type,
           CASE WHEN starts_with(task_name, 'task_') THEN 'independent'
                WHEN task_name = 'MergeTask' THEN 'Merge'
                ELSE regexp_extract(task_name, '^([A-Z])', 1) END AS op,
           list_transform(string_split(regexp_replace(task_name, '_Stg[0-9]+$', ''), '_')[2:],
                          x -> TRY_CAST(x AS INT)) AS parents
    FROM {TASKS}
    WHERE hash(job_name) % 100 < 5 AND plan_cpu > 0 AND plan_mem > 0 AND instance_num > 0
""")
con.execute("UPDATE tasks SET parents = [] WHERE op IN ('independent', 'Merge')")

con.execute(f"""
    CREATE TABLE task_stats AS
    WITH i AS (
        SELECT job_name, task_name, status, cpu_avg, mem_max, start_time, end_time,
               end_time - start_time AS d,
               median(end_time - start_time) OVER (PARTITION BY job_name, task_name) AS task_median
        FROM {INSTANCES}
        WHERE hash(job_name) % 100 < 5 AND start_time > 0 AND end_time >= start_time
    )
    SELECT job_name, task_name, count(*) AS n, median(cpu_avg) AS cpu_used, median(mem_max) AS mem_used,
           median(d) AS d50, quantile_cont(d, 0.9) AS d90,
           avg((d >= 1.5 * task_median AND d - task_median >= 2)::INT) AS straggler_share,
           avg((status = 'Failed')::INT) AS failed_share,
           min(start_time) AS first_start, max(end_time) AS last_end
    FROM i GROUP BY job_name, task_name
""")

con.execute("""
    CREATE TABLE jobs AS
    WITH referenced AS (
        SELECT job_name, count(DISTINCT p) AS n_with_children
        FROM (SELECT job_name, unnest(parents) AS p FROM tasks) GROUP BY job_name
    )
    SELECT t.job_name,
           ln(avg(t.plan_cpu)) AS log_cpu, ln(avg(t.plan_mem)) AS log_mem,
           ln(sum(t.instance_num)) AS log_instances, ln(max(t.instance_num)) AS log_max_instances,
           ln(0.01 + median(s.cpu_used / t.plan_cpu)) AS log_cpu_used_ratio,
           ln(0.01 + median(s.mem_used / t.plan_mem)) AS log_mem_used_ratio,
           ln(1 + median(s.d50)) AS log_duration,
           avg(ln((1 + s.d90) / (1 + s.d50))) AS duration_spread,
           sum(s.straggler_share * s.n) / sum(s.n) AS straggler_share,
           ln(1 + max(s.last_end) - min(s.first_start)) AS log_span,
           sum(s.failed_share * s.n) / sum(s.n) AS failed_share,
           ln(count(*)) AS log_tasks,
           avg(len(t.parents)) AS mean_parents,
           avg((len(t.parents) = 0)::INT) AS root_share,
           least(coalesce(any_value(r.n_with_children), 0) / count(*), 1) AS parent_share,
           CASE WHEN bool_and(t.op = 'independent') THEN 'independent'
                ELSE array_to_string(list_sort(list_distinct(list(t.op) FILTER (WHERE t.op <> 'independent'))), '+')
           END AS op_mix,
           mode(t.task_type) AS main_task_type
    FROM tasks t
    JOIN task_stats s USING (job_name, task_name)
    LEFT JOIN referenced r USING (job_name)
    GROUP BY t.job_name
""")
# Jobs without any usage measurement are excluded (NULL stays NULL, never 0).
con.execute(f"""
    COPY (SELECT * FROM jobs WHERE {" AND ".join(f + " IS NOT NULL" for f in FEATURES)} ORDER BY job_name)
    TO 'data/parquet/job_features.parquet'
""")
data = con.execute("""
    SELECT *, hash(job_name || 'test') % 5 = 0 AS is_test FROM 'data/parquet/job_features.parquet' ORDER BY job_name
""").fetchnumpy()
excluded = con.execute("SELECT count(*) FROM jobs").fetchone()[0] - len(data["job_name"])
print(f"{len(data['job_name']):,} jobs, {excluded:,} excluded (no usage measurement)")

# Each block gets the same total weight. failed_share is left out: it makes clusters less stable.
columns = []
for block in BLOCKS.values():
    kept = [f for f in block if f != "failed_share"]
    for feature in kept:
        x = data[feature].astype(float)
        columns.append((x - x.mean()) / x.std() / np.sqrt(len(kept)))
X = np.column_stack(columns)

pca = PCA().fit(X)
explained = np.cumsum(pca.explained_variance_ratio_)
n_components = int(np.searchsorted(explained, 0.9)) + 1
Z = pca.transform(X)[:, :n_components]

rng = np.random.default_rng(0)
half_a, half_b = np.split(rng.permutation(len(Z))[:100_000], 2)
check = half_a[:20_000]

K = 6  # silhouette is flat from 3 to 7; k = 6 is as stable as k = 3 and more detailed

# Stability: fit on two disjoint halves, label all jobs with both, compare (ARI 1 = identical).
kmeans_b = KMeans(n_clusters=K, n_init=10, random_state=1).fit(Z[half_b])
gmm_b = GaussianMixture(n_components=K, random_state=1).fit(Z[half_b])
kmeans = KMeans(n_clusters=K, n_init=10, random_state=0).fit(Z[half_a]).predict(Z)
kmeans = np.argsort(np.argsort(-np.bincount(kmeans)))[kmeans]  # cluster 0 = largest
gmm = GaussianMixture(n_components=K, random_state=0).fit(Z[half_a]).predict(Z)
hdbscan_rows = rng.choice(len(Z), size=40_000, replace=False)
hdbscan = HDBSCAN(min_cluster_size=200, copy=True).fit_predict(Z[hdbscan_rows])
noise = hdbscan == -1

# HDBSCAN's silhouette ignores its noise points, so KMeans is also scored on those same points.
kept = hdbscan_rows[~noise][:20_000]
names = ["KMeans\n(all jobs)", "GMM\n(all jobs)", "KMeans\n(non-noise jobs)", "HDBSCAN\n(non-noise jobs)"]
silhouettes = [silhouette_score(Z[check], kmeans[check]), silhouette_score(Z[check], gmm[check]),
               silhouette_score(Z[kept], kmeans[kept]), silhouette_score(Z[kept], hdbscan[~noise][:20_000])]
stabilities = [adjusted_rand_score(kmeans, kmeans_b.predict(Z)), adjusted_rand_score(gmm, gmm_b.predict(Z))]
fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4))
left.bar(names, silhouettes, color=["tab:blue", "tab:orange", "tab:blue", "tab:green"])
left.tick_params(axis="x", labelsize=8)
left.set_ylabel("silhouette")
left.set_title(f"Separation (HDBSCAN: {len(set(hdbscan)) - 1} clusters, {100 * noise.mean():.0f} % noise)")
right.bar(names[:2], stabilities, color=["tab:blue", "tab:orange"])
right.set_ylim(0, 1)
right.set_ylabel("ARI between two disjoint halves")
right.set_title("Stability (1 = same clusters)")
for axis, values in [(left, silhouettes), (right, stabilities)]:
    for i, value in enumerate(values):
        axis.text(i, value + 0.01, f"{value:.2f}", ha="center")
save("jobs_method_comparison")

pq.write_table(pa.table({"job_name": data["job_name"], "cluster": kmeans}), "data/parquet/job_clusters.parquet")

with open("results/representative_jobs.csv", "w") as f:
    f.write("cluster,family,job_name,distance_to_center\n")
    for c in range(K):
        rows = np.flatnonzero(kmeans == c)
        distance = np.linalg.norm(Z[rows] - Z[rows].mean(axis=0), axis=1)
        for row, d in zip(rows[np.argsort(distance)[:5]], np.sort(distance)[:5]):
            f.write(f"{c},{FAMILIES[c]},{data['job_name'][row]},{d:.3f}\n")

shown = rng.choice(len(Z), size=10_000, replace=False)
xy = TSNE(random_state=0).fit_transform(Z[shown])
plt.figure(figsize=(10, 7))
for family, name in enumerate(FAMILIES):
    rows = kmeans[shown] == family
    plt.scatter(xy[rows, 0], xy[rows, 1], s=4, label=f"{name} ({100 * rows.mean():.0f} %)")
plt.legend(markerscale=4, fontsize=11)
plt.xticks([])
plt.yticks([])
plt.title(f"Each point is a job (10,000 jobs), colored by its family (KMeans, k = {K})")
save("jobs_family_map")

# Recognizable before launch? Only requested resources and DAG structure, tested on unseen jobs.
X = np.column_stack([data[f] for f in BLOCKS["requested"] + BLOCKS["structure"]])
train, test = ~data["is_test"], data["is_test"]
predicted = HistGradientBoostingClassifier(random_state=0).fit(X[train], kmeans[train]).predict(X[test])
truth = kmeans[test]
confusion = np.array([[np.sum((truth == a) & (predicted == b)) for b in range(K)] for a in range(K)])
shares = 100 * confusion / confusion.sum(axis=1, keepdims=True)
plt.figure(figsize=(8, 6))
plt.imshow(shares, cmap="Blues", vmin=0, vmax=100)
for a in range(K):
    for b in range(K):
        plt.text(b, a, f"{shares[a, b]:.0f}", ha="center", va="center")
plt.colorbar(label="% of the family's jobs")
plt.xticks(range(K), FAMILIES, rotation=30, ha="right")
plt.yticks(range(K), FAMILIES)
plt.xlabel("family predicted before launch")
plt.ylabel("real family")
plt.title(f"Recognizing the family before launch (accuracy {100 * np.mean(truth == predicted):.0f} %)")
save("jobs_recognition")
