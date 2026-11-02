# Source-only recipe. Build only after the exact context and disk allocation are released.
# Resolve this build reference to the reviewed local base image ID before build.
# Required ID: sha256:e3ff7b02740b55f52735b17afc9846d151f86bf7395341f5d53f7d3dbb235cfc
ARG REVIEWED_BASE
FROM ${REVIEWED_BASE}
USER 0:0
COPY mutagen-1.48.1-py3-none-any.whl /evaluation-dependencies/
COPY mutagen-1.48.1.tar.gz /evaluation-dependencies/
COPY COPYING NOTICE.md /evaluation-dependencies/
RUN uv pip install --python /usr/local/bin/python --no-index --no-deps \
    /evaluation-dependencies/mutagen-1.48.1-py3-none-any.whl
USER 1000:1000
WORKDIR /source
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/source/src:/source LANG=C LC_ALL=C TERM=dumb
ENTRYPOINT ["/usr/local/bin/python", "-B", "-m", "evaluations.fma_validation.owner"]
CMD ["--execute-reviewed-packet"]
