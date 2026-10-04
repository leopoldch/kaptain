# Do typical mixes of tasks (LDA topics, job = document, task = word) match the job families?

import matplotlib.pyplot as plt
import numpy as np
from sklearn.decomposition import LatentDirichletAllocation
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.metrics import normalized_mutual_info_score

from common import TASKS, connect, save

TOPICS = 6

docs = connect().execute(f"""
    WITH words AS (
        SELECT job_name,
               CASE WHEN starts_with(task_name, 'task_') THEN 'indep'
                    WHEN task_name = 'MergeTask' THEN 'merge'
                    ELSE regexp_extract(task_name, '^([A-Z])', 1) END
               || '_' || CASE WHEN instance_num = 1 THEN 'one' WHEN instance_num <= 100 THEN 'few' ELSE 'many' END
               || '_' || CASE WHEN starts_with(task_name, 'task_') OR task_name = 'MergeTask' THEN 'p0'
                              ELSE 'p' || least(len(string_split(regexp_replace(task_name, '_Stg[0-9]+$', ''), '_')) - 1, 2)
                         END AS word
        FROM {TASKS}
        WHERE hash(job_name) % 100 < 5 AND plan_cpu > 0 AND plan_mem > 0 AND instance_num > 0
    )
    SELECT w.job_name, string_agg(w.word, ' ') AS text, any_value(c.cluster) AS cluster, any_value(f.op_mix) AS op_mix
    FROM words w
    JOIN 'data/parquet/job_clusters.parquet' c USING (job_name)
    JOIN 'data/parquet/job_features.parquet' f USING (job_name)
    GROUP BY w.job_name ORDER BY w.job_name
""").fetchnumpy()

vectorizer = CountVectorizer(token_pattern=r"\S+", lowercase=False)
counts = vectorizer.fit_transform(docs["text"])
words = vectorizer.get_feature_names_out()

lda = LatentDirichletAllocation(n_components=TOPICS, random_state=0).fit(counts)
topic = lda.transform(counts).argmax(axis=1)
weights = lda.components_ / lda.components_.sum(axis=1, keepdims=True)
top = np.argsort(-weights.max(axis=0))[:15]
plt.figure(figsize=(12, 4))
plt.imshow(weights[:, top], cmap="Greens", aspect="auto")
plt.colorbar(label="weight of the word in the topic")
plt.xticks(range(len(top)), words[top], rotation=45, ha="right")
plt.yticks(range(TOPICS), [f"topic {t} ({100 * (topic == t).mean():.0f} % of jobs)" for t in range(TOPICS)])
plt.title("LDA topics: typical mixes of tasks (word = operator_size_parents)")
save("topics_words")

K = docs["cluster"].max() + 1
table = np.array([[np.sum((topic == t) & (docs["cluster"] == c)) for c in range(K)] for t in range(TOPICS)])
shares = 100 * table / table.sum(axis=0, keepdims=True)
plt.figure(figsize=(8, 5))
plt.imshow(shares, cmap="Blues", vmin=0, vmax=100)
for t in range(TOPICS):
    for c in range(K):
        plt.text(c, t, f"{shares[t, c]:.0f}", ha="center", va="center")
plt.colorbar(label="% of the cluster's jobs")
plt.xticks(range(K), [f"cluster {c}" for c in range(K)])
plt.yticks(range(TOPICS), [f"topic {t}" for t in range(TOPICS)])
plt.title(f"Topics vs clusters (NMI {normalized_mutual_info_score(docs['cluster'], topic):.2f}), "
          f"topics vs operator mix (NMI {normalized_mutual_info_score(docs['op_mix'], topic):.2f})")
save("topics_vs_clusters")
