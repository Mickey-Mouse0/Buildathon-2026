# The scorer builds this on linux/amd64 and runs it with no network. The digest
# is the image the answers were labeled in: keep it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot's source at the pinned commit, read by the static analyzer.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
# Compiled here so neither the selector nor the import-diff child compiles sqlglot
# on every run (PYTHONDONTWRITEBYTECODE below means nothing would be cached).
RUN python /app/tools/fetch_sqlglot.py /opt/sqlglot \
    && python -c "import compileall; compileall.compile_dir('/opt/sqlglot/sqlglot', quiet=1)" \
    && chmod -R a+rX /opt/sqlglot

# DuckDB, pandas, numpy and pytz at the versions the answers were labeled with, for the
# check of edited TPC-DS queries (blast/tpcquery.py). They go in a folder of their
# own that only that check puts on its path, so nothing else sees them.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore --target /opt/tpcdeps -c /app/requirements.lock \
        duckdb pandas numpy pytz \
    && chmod -R a+rX /opt/tpcdeps

COPY blast /app/blast
COPY tools/build_importstate.sh /app/tools/build_importstate.sh
# The pinned commit's result for every recorded tokenizer call, made in this image
# so the selector's replays of the patched tokenizer compare like with like. The replay
# is off by default (BLAST_TOKREPLAY_ON), and without this file it stays off, so the
# 36 s it takes are spent only when asked for: --build-arg BLAST_TOKREPLAY_BUILD=1
ARG BLAST_TOKREPLAY_BUILD=0
RUN sh /app/tools/build_importstate.sh /opt/sqlglot /app/blast/importstate.pkl \
    && { [ "$BLAST_TOKREPLAY_BUILD" != 1 ] \
         || PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 python /app/blast/tokreplay.py base /opt/sqlglot \
            /app/blast/tokreplay.pkl.xz /app/blast/tokreplay_base.pkl; } \
    && PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 python /app/blast/tpcmap.py expected /opt/sqlglot /app/blast/tpc_expected.pkl \
    && (PYTHONDONTWRITEBYTECODE=1 python /app/blast/tpcmap.py rows /opt/sqlglot /app/blast/tpc_rows.pkl \
        || (rm -f /app/blast/tpc_rows.pkl; echo "tpc rows failed: the check parses the fixture tables itself")) \
    && (PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 python /app/blast/tpcmap.py setup-lines /opt/sqlglot /app/blast/tpc_setup_lines.json \
        || echo "tpc setup-lines failed: every TPC check rebuilds the tables") \
    && PYTHONDONTWRITEBYTECODE=1 python /app/blast/tpcquery.py known /opt/sqlglot /app/blast/tpc_known.json \
    && (PYTHONDONTWRITEBYTECODE=1 python /app/blast/hlreplay.py record /opt/sqlglot /app/blast/maxnodes_hl.pkl \
        || echo "max_nodes recording failed: the replay stays off") \
    && python -c "import compileall; compileall.compile_dir('/opt/sqlglot/tests', quiet=1)" \
    && (PYTHONDONTWRITEBYTECODE=1 python /app/blast/runcheck.py record /opt/sqlglot /opt/tpcdeps /app/blast \
        || (rm -f /app/blast/runcheck_called.pkl /app/blast/runcheck_slices.pkl; echo "runtime check recording failed: the check stays off")) \
    && python /app/blast/prover/shard.py /app/blast/prover_data/profiles.pkl.xz /app/blast/prover_data/target_index.json.gz \
       /opt/sqlglot /app/blast/prover_data/profiles.shards \
    && python -c "import lzma, shutil; shutil.copyfileobj(lzma.open('/app/blast/prover_data/union.pkl.xz'), open('/app/blast/prover_data/union.pkl', 'wb'))" \
    && python /app/blast/prover/prove.py reads-index /opt/sqlglot /app/blast/prover_data/reads_index.pkl \
    && rm /app/blast/prover_data/profiles.pkl.xz /app/blast/prover_data/union.pkl.xz \
    && python -c "import sys; sys.path.insert(0, '/app/blast'); import analyze; analyze.split_map('/app/blast/covmap.pkl.gz')" \
    && chmod -R a+rX /app \
    && python -c "import compileall; compileall.compile_dir('/app/blast', quiet=1)"

ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/blast/selector.py"]
