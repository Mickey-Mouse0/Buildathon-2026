# The scorer builds this on linux/amd64 and runs it with no network. The digest
# is the image the answers were labeled in: keep it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot's source at the pinned commit, read by the static analyzer.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /opt/sqlglot && chmod -R a+rX /opt/sqlglot

COPY blast /app/blast
RUN python -c "import lzma, shutil; shutil.copyfileobj(lzma.open('/app/blast/prover_data/profiles.pkl.xz'), open('/app/blast/prover_data/profiles.pkl', 'wb'))" \
    && python -c "import lzma, shutil; shutil.copyfileobj(lzma.open('/app/blast/prover_data/union.pkl.xz'), open('/app/blast/prover_data/union.pkl', 'wb'))" \
    && rm /app/blast/prover_data/profiles.pkl.xz /app/blast/prover_data/union.pkl.xz \
    && chmod -R a+rX /app \
    && python -c "import compileall; compileall.compile_dir('/app/blast', quiet=1)"

ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/blast/selector.py"]
