"""Record the per-query CPU cost of the TPC executor queries and pin it in the repo.

    python tools/record_tpc_cpu.py <sqlglot dir> <out json> [runs] [total for test_execute_tpcds]

Runs `blast/tpcmap.py expected` <runs> times and keeps each query's median "cpu" and
"front" seconds. The selector's TPC check (tpc_check) uses these to decide whether
running the check is worth it and in which order to run the queries.

They used to be measured once at image build, which swung test_execute_tpcds's total
between about 13.8 and 18.3 s from build to build, right around the gate's cutoff
(about 16.7 s for its 21.5 s), so the check ran on some builds and not others. Pinned,
every build decides the same way.

The optional total rescales test_execute_tpcds's queries (cpu and front alike) so they
add up to it. We pin it under the cutoff on purpose: a check over all its queries costs
about 18 s of CPU to drop a 21.5 s test, a small net win, so we want it to run every
time. The relative cost of the queries, which sets their order, is what was measured.

Record it inside the scorer's image (CLAUDE.md), for example:
    docker run --rm --platform linux/amd64 --cpus 2 -v $PWD:/repo -w /repo <built image> \
        python tools/record_tpc_cpu.py /opt/sqlglot blast/tpc_cpu.json 5 15.0
"""
import json
import os
import pickle
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def main(src, out, runs="5", total=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHONHASHSEED="0")
    tpcmap = os.path.join(HERE, "..", "blast", "tpcmap.py")
    recs = []
    with tempfile.TemporaryDirectory() as tmp:
        for k in range(int(runs)):
            path = os.path.join(tmp, f"e{k}.pkl")
            subprocess.run([sys.executable, tpcmap, "expected", src, path], env=env, check=True)
            with open(path, "rb") as f:
                recs.append(pickle.load(f))
            print(f"run {k}: {TPCDS.rsplit('::', 1)[1]} {sum(r['cpu'] for r in recs[-1][TPCDS]):.2f}s", file=sys.stderr)
    result = {}
    for test_id in recs[0]:
        n = len(recs[0][test_id])
        result[test_id] = {key: [round(statistics.median(r[test_id][i][key] for r in recs), 4) for i in range(n)]
                           for key in ("cpu", "front")}
    measured = sum(result[TPCDS]["cpu"])
    meta = {"runs": int(runs), "measured_total": round(measured, 2)}
    if total is not None:
        k = float(total) / measured
        for key in ("cpu", "front"):
            result[TPCDS][key] = [round(x * k, 4) for x in result[TPCDS][key]]
        meta["pinned_total"] = float(total)
    with open(out, "w") as f:
        json.dump({"_meta": meta, **result}, f, indent=1)
        f.write("\n")
    print(f"median total {measured:.2f}s; pinned {sum(result[TPCDS]['cpu']):.2f}s -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
