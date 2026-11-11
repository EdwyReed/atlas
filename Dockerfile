FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS build
WORKDIR /build
COPY requirements-build.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.lock
COPY pyproject.toml README.md ./
COPY atlas_engine ./atlas_engine
RUN pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /wheels .

FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1
COPY requirements-cpu.lock /opt/atlas/requirements-cpu.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r /opt/atlas/requirements-cpu.lock
COPY --from=build /wheels /opt/atlas/wheels
RUN pip install --no-cache-dir --no-deps /opt/atlas/wheels/*.whl \
    && groupadd --gid 10001 atlas && useradd --uid 10001 --gid 10001 --no-create-home atlas \
    && mkdir -p /var/lib/atlas /run/atlas /home/atlas \
    && chown 10001:10001 /var/lib/atlas /home/atlas
USER 10001:10001
ENV HOME=/home/atlas
WORKDIR /home/atlas
VOLUME ["/var/lib/atlas"]
EXPOSE 12088
ENTRYPOINT ["atlas"]
CMD ["--config", "/run/atlas/atlas.json"]
