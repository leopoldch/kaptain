# Which job representation predicts real usage best (train days 0-5, test days 5.5-7.75)?

import os

import matplotlib.pyplot as plt
import numpy as np
from fastembed import TextEmbedding
from sklearn.decomposition import PCA, TruncatedSVD
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.neural_network import MLPRegressor

from common import TASKS, connect, save

BEFORE_LAUNCH = ["log_cpu", "log_mem", "log_instances", "log_max_instances",
                 "log_tasks", "mean_parents", "root_share", "parent_share"]
TARGETS = {"log_mem_used_ratio": "memory used / requested",
           "log_cpu_used_ratio": "CPU used / requested",
           "log_duration": "duration"}
CACHE = "data/job_text_embeddings.npz"

jobs = connect().execute(f"""
    WITH tasks AS (
        SELECT job_name, task_name, start_time,
               CASE WHEN starts_with(task_name, 'task_') THEN 'independent'
                    WHEN task_name = 'MergeTask' THEN 'merge'
                    ELSE regexp_extract(task_name, '^([A-Z])', 1) END AS op,
               CASE WHEN starts_with(task_name, 'task_') OR task_name = 'MergeTask' THEN 0
                    ELSE len(string_split(regexp_replace(task_name, '_Stg[0-9]+$', ''), '_')) - 1 END AS parents,
               instance_num, plan_cpu, plan_mem,
               row_number() OVER (PARTITION BY job_name ORDER BY task_name) AS rank
        FROM {TASKS}
        WHERE hash(job_name) % 100 < 5 AND plan_cpu > 0 AND plan_mem > 0 AND instance_num > 0
    ),
    words AS (
        SELECT job_name, min(start_time) AS submit_time,
               string_agg(op || '_' || CASE WHEN instance_num = 1 THEN 'one' WHEN instance_num <= 100 THEN 'few'
                                           ELSE 'many' END || '_p' || least(parents, 2), ' ') AS words,
               'Batch job with ' || count(*) || ' tasks. ' ||
               string_agg(op || ' task with ' || instance_num || ' instances, ' || plan_cpu / 100 || ' cores, memory '
                          || plan_mem || ', ' || parents || ' parents', '. ' ORDER BY task_name)
                   FILTER (WHERE rank <= 10) AS text
        FROM tasks GROUP BY job_name
    )
    SELECT f.*, w.words, w.text, (w.submit_time % 86400) // 3600 AS hour,
           w.submit_time < 5 * 86400 AS is_train,
           w.submit_time >= 5.5 * 86400 AND w.submit_time < 7.75 * 86400 AS is_test
    FROM 'data/parquet/job_features.parquet' f JOIN words w USING (job_name)
    ORDER BY job_name
""").fetchnumpy()
train, test = jobs["is_train"], jobs["is_test"]
hour = jobs["hour"].reshape(-1, 1).astype(float)
raw = np.column_stack([jobs[f] for f in BEFORE_LAUNCH])
scaled = (raw - raw[train].mean(axis=0)) / raw[train].std(axis=0)
rng = np.random.default_rng(0)
sample = rng.choice(np.flatnonzero(train), size=50_000, replace=False)

pca_embedding = PCA(n_components=0.9).fit(scaled[train]).transform(scaled)

# Autoencoder: a small network rebuilds the features through a 4-number bottleneck.
autoencoder = MLPRegressor(hidden_layer_sizes=(16, 4, 16), max_iter=200, random_state=0)
autoencoder.fit(scaled[sample], scaled[sample])
hidden = np.maximum(scaled @ autoencoder.coefs_[0] + autoencoder.intercepts_[0], 0)
autoencoder_embedding = np.maximum(hidden @ autoencoder.coefs_[1] + autoencoder.intercepts_[1], 0)

# Task composition: each job is a bag of task words (operator_size_parents), TF-IDF then SVD.
tfidf = TfidfVectorizer(token_pattern=r"\S+", lowercase=False).fit(jobs["words"][train])
composition_embedding = TruncatedSVD(n_components=8, random_state=0).fit_transform(tfidf.transform(jobs["words"]))

# LLM text embedding of a sentence describing the job (first 10 tasks); slow, so cached in data/.
if os.path.exists(CACHE):
    cache = np.load(CACHE)
    vector_of = dict(zip(cache["texts"], cache["vectors"]))
else:
    texts = list(dict.fromkeys(jobs["text"]))
    model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2", cache_dir="data/fastembed_cache")
    vectors = np.array(list(model.embed(texts)))
    np.savez(CACHE, texts=np.array(texts), vectors=vectors)
    vector_of = dict(zip(texts, vectors))
llm_vectors = np.array([vector_of[text] for text in jobs["text"]])
llm_embedding = PCA(n_components=16).fit(llm_vectors[train]).transform(llm_vectors)

representations = {
    "raw features": scaled,
    "PCA": pca_embedding,
    "autoencoder\n(4 dims)": autoencoder_embedding,
    "task composition\n(TF-IDF + SVD)": composition_embedding,
    "LLM text\nembedding": llm_embedding,
    "raw features\n+ LLM": np.column_stack([scaled, llm_embedding]),
}

# Unseen = test jobs whose before-launch profile never appears in training.
profile = [tuple(np.round(r, 3)) for r in raw]
train_profiles = {profile[i] for i in np.flatnonzero(train)}
unseen = test & np.array([p not in train_profiles for p in profile])


def r2(y, prediction):
    return 1 - ((y - prediction) ** 2).mean() / ((y - y.mean()) ** 2).mean()


names = list(representations)
positions = np.arange(len(names))
fig, axes = plt.subplots(1, 3, figsize=(20, 5), sharey=True)
for axis, target in zip(axes, TARGETS):
    y = jobs[target]
    scores = []
    for embedding in representations.values():
        X = np.column_stack([embedding, hour])
        prediction = HistGradientBoostingRegressor(random_state=0).fit(X[train], y[train]).predict(X)
        scores.append((r2(y[test], prediction[test]), r2(y[unseen], prediction[unseen])))
    for shift, index, label in [(-0.2, 0, "all test jobs"), (0.2, 1, f"unseen profiles ({unseen.sum():,} jobs)")]:
        values = [s[index] for s in scores]
        axis.bar(positions + shift, values, width=0.4, label=label)
        for i, value in enumerate(values):
            axis.text(i + shift, max(value, 0) + 0.01, f"{value:.2f}", ha="center", fontsize=7)
    axis.set_xticks(positions, names, fontsize=8)
    axis.set_ylim(0, 1)
    axis.set_title(f"Predicting {TARGETS[target]}")
axes[0].set_ylabel("R² on later days (1 = perfect)")
axes[0].legend(fontsize=8)
fig.suptitle("Which job representation predicts real usage best? (gradient boosting, temporal split)")
save("embeddings_prediction")
