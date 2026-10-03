# Converts the raw CSV tables to Parquet and extracts container_usage for 10 % of the machines.

import os
import subprocess

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as csv
import pyarrow.parquet as pq

TABLES = {
    "machine_meta": {"machine_id": "VARCHAR", "time_stamp": "BIGINT", "failure_domain_1": "BIGINT",
                     "failure_domain_2": "VARCHAR", "cpu_num": "BIGINT", "mem_size": "BIGINT", "status": "VARCHAR"},
    "container_meta": {"container_id": "VARCHAR", "machine_id": "VARCHAR", "time_stamp": "BIGINT",
                       "app_du": "VARCHAR", "status": "VARCHAR", "cpu_request": "DOUBLE", "cpu_limit": "DOUBLE",
                       "mem_size": "DOUBLE"},
    "batch_task": {"task_name": "VARCHAR", "instance_num": "BIGINT", "job_name": "VARCHAR", "task_type": "VARCHAR",
                   "status": "VARCHAR", "start_time": "BIGINT", "end_time": "BIGINT", "plan_cpu": "DOUBLE",
                   "plan_mem": "DOUBLE"},
    "machine_usage": {"machine_id": "VARCHAR", "time_stamp": "BIGINT", "cpu_util_percent": "DOUBLE",
                      "mem_util_percent": "DOUBLE", "mem_gps": "DOUBLE", "mpki": "DOUBLE", "net_in": "DOUBLE",
                      "net_out": "DOUBLE", "disk_io_percent": "DOUBLE"},
    "batch_instance": {"instance_name": "VARCHAR", "task_name": "VARCHAR", "job_name": "VARCHAR",
                       "task_type": "VARCHAR", "status": "VARCHAR", "start_time": "BIGINT", "end_time": "BIGINT",
                       "machine_id": "VARCHAR", "seq_no": "BIGINT", "total_seq_no": "BIGINT", "cpu_avg": "DOUBLE",
                       "cpu_max": "DOUBLE", "mem_avg": "DOUBLE", "mem_max": "DOUBLE"},
}

os.makedirs("data/parquet", exist_ok=True)
for name, columns in TABLES.items():
    if not os.path.exists(f"data/parquet/{name}.parquet"):
        table = duckdb.read_csv(f"data/raw/{name}.csv", header=False, names=list(columns), dtype=columns)
        table.write_parquet(f"data/parquet/{name}.parquet")

# container_usage is a 176 GB CSV inside a 28 GB tar.gz, too big to unpack: stream it instead.
CONTAINER_USAGE = "data/parquet/container_usage_10pct.parquet"
if not os.path.exists(CONTAINER_USAGE):
    columns = ["container_id", "machine_id", "time_stamp", "cpu_util_percent", "mem_util_percent",
               "cpi", "mem_gps", "mpki", "net_in", "net_out", "disk_io_percent"]
    types = {c: pa.float64() for c in columns}
    types.update(container_id=pa.string(), machine_id=pa.string(), time_stamp=pa.int64())
    machines = duckdb.sql("""
        SELECT DISTINCT machine_id FROM 'data/parquet/machine_meta.parquet' WHERE hash(machine_id) % 10 = 0
    """).fetchnumpy()["machine_id"]
    kept_machines = pa.array(machines.tolist())

    tar = subprocess.Popen(["tar", "-xzOf", "data/raw/container_usage.tar.gz"], stdout=subprocess.PIPE)
    reader = csv.open_csv(tar.stdout, read_options=csv.ReadOptions(column_names=columns, block_size=64 << 20),
                          convert_options=csv.ConvertOptions(column_types=types))
    writer = pq.ParquetWriter(CONTAINER_USAGE, pa.schema([(c, types[c]) for c in columns]))
    for batch in reader:
        writer.write_batch(batch.filter(pc.is_in(batch["machine_id"], value_set=kept_machines)))
    writer.close()
    tar.wait()
