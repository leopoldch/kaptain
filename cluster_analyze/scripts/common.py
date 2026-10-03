import os

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

os.makedirs("results/figures", exist_ok=True)

INSTANCES = "'data/parquet/batch_instance.parquet'"
TASKS = "'data/parquet/batch_task.parquet'"
USAGE = "'data/parquet/machine_usage.parquet'"
TWINS = "'data/parquet/twins.parquet'"

# Days 1.6-1.9 have no measurements and the trace ends abnormally after day 7.75 (see explore.py).
HOLE_START, HOLE_END, TRACE_END = 138240, 164160, 669600


def connect():
    con = duckdb.connect()
    con.execute("SET memory_limit = '6GB'")
    con.execute("SET temp_directory = 'data/duckdb_tmp'")
    con.execute("SET enable_progress_bar = false")
    return con


def save(name):
    plt.tight_layout()
    plt.savefig(f"results/figures/{name}.png", dpi=120)
    plt.close()


def to_percent(log_gap):
    return 100 * (np.exp(log_gap) - 1)
