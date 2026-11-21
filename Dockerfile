# FAST Team - Blast Radius Selector
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Fetch sqlglot at pinned commit
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python - <<EOF
import io, os, tarfile, urllib.request
sha = "${SQLGLOT_SHA}"
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=300).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall("/opt", filter="data")
os.rename(f"/opt/sqlglot-{sha}", "/opt/sqlglot")
EOF

COPY selector.py /app/selector.py
COPY analysis /app/analysis
COPY maps /app/maps
COPY pickle_maps.py /tmp/pickle_maps.py
COPY build_source_reader_map.py /tmp/build_source_reader_map.py
RUN python /tmp/pickle_maps.py /app/maps
RUN python /tmp/build_source_reader_map.py /opt/sqlglot /app/maps
RUN python -m compileall -q /app /opt/sqlglot/sqlglot && chmod -R a+rX /app /opt/sqlglot

ENV BR_SQLGLOT=/opt/sqlglot
CMD ["python", "/app/selector.py"]
