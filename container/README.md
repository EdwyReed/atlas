# CPU container (development slice)

Build from the repository root. No private build paths, Hermes, InvokeAI, vocabulary,
models or weights are needed. Linux amd64 / Python 3.11 is exercised; other platforms
are not accepted. The base image is digest-pinned; both dependency inputs use exact
versions and hashes. The local Atlas wheel is built without dependency resolution
or build isolation. This is not a bit-for-bit image reproducibility or licensing audit.

```sh
docker build -t atlas-core:cpu-slice .
```

## Isolated ordinary bridge run

Choose a free loopback port and a NEW resource name. Keep the shell variables for
later stop/start; do not run the initializer on an existing config volume.

```sh
NAME=atlas-demo-$(python3 -c 'import uuid;print(uuid.uuid4().hex[:12])')
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
docker network create "$NAME"
docker volume create "$NAME-state"
docker volume create "$NAME-config"
docker run --rm -i --network none --user 0:0 \
  -v "$NAME-config:/run/atlas" --entrypoint python atlas-core:cpu-slice \
  - "$PORT" < container/init.py
docker run -d --name "$NAME" --network "$NAME" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --cpus 1 --pids-limit 128 \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  -p "127.0.0.1:$PORT:12088" \
  -v "$NAME-state:/var/lib/atlas" -v "$NAME-config:/run/atlas:ro" \
  atlas-core:cpu-slice
docker inspect --format '{{.Config.User}} {{.State.Status}}' "$NAME"
docker port "$NAME"
```

Only the one-shot config-volume initializer uses namespace-root. It creates private
0600 files and a 0700 directory owned by 10001 inside the volume, also on rootless
Docker; no host chown or permissive secret-file modes are needed. The daemon always
runs as 10001:10001. A new state volume inherits the image directory ownership.
Do not override USER or attach an existing unrelated volume to fix permissions.
Secrets are mounted files, never build inputs or environment tokens. The config
volume is private runtime material and needs the same protection as any credential.

There is no automatic restart policy or live deployment. `docker stop "$NAME"` and
`docker start "$NAME"` retain both volumes. Deleting the container alone does not
remove state. The initializer intentionally refuses a nonempty config volume.

For the installed host CLI, copy only this newly generated fixture credential into
a private directory OUTSIDE the repository (never use a browser/profile credential):

```sh
# Set RUN_DIR to a new private runtime directory outside source.
(umask 077; mkdir "$RUN_DIR")
docker cp "$NAME:/run/atlas/token" "$RUN_DIR/token"
chmod 600 "$RUN_DIR/token"
atlas-client --endpoint "http://127.0.0.1:$PORT" --token-file "$RUN_DIR/token" status
```

Use the same endpoint for HTTP and `/mcp/`. HTTP/MCP/CLI operations are documented in
the main README. In container mode the gate accepts ONLY the exact configured
`http://127.0.0.1:HOST_PORT` Origin and corresponding Host. Non-browser clients omit
Origin. Even `localhost`, the container IP and internal port Host are refused.
`container_endpoint` requires explicit `host: "0.0.0.0"`; omitting it preserves the
loopback-only launcher. The application cannot enforce Docker publish addresses:
**keep `-p 127.0.0.1:...` and never expose this slice on a public interface.**
No proxy or host networking is required. Ordinary bridge networking is used because
Docker internal networks do not provide the required published port on the tested
rootless Engine. This is not a runtime egress firewall.

When this disposable run is no longer needed, remove only its exact resources;
volume removal destroys its synthetic data and credential:

```sh
docker rm -f "$NAME"
docker network rm "$NAME"
docker volume rm "$NAME-state" "$NAME-config"
rm "$RUN_DIR/token"
rmdir "$RUN_DIR"
```

## Repeat the real boundary acceptance

Install the current wheel into a separate host environment, as in the main README.
The host CLI and real MCP SDK connect through the published loopback port to the
exact built image. Installed host, wheel, source and container Python module bytes
must match. Synthetic state and secrets start on clean uniquely named volumes;
restart/readback/intent replay and owner contention run against that container.
The runner removes only its owned containers/network/volumes, including on failure.

```sh
/absolute/venv/bin/python -I -B verification/acceptance.py \
  --docker-image atlas-core:cpu-slice \
  --wheel /absolute/path/to/atlas_engine-0.4.0.dev1-py3-none-any.whl \
  --out /absolute/new/private-container-acceptance-directory
```

Evidence includes `result.json`, `container.json` (image ID, UID, module hashes,
exact create command and port bindings), `container.log`, and `cleanup.json`.
The daemon uses the image's installed `atlas` entrypoint, not source mounts. No
external backend is configured or invoked; unlike self-host mode, Docker mode does
not install a Python child egress audit hook. The ordinary non-Docker acceptance
continues to exercise that guard. This is core corpus acceptance, not a full legacy
operation/backend, populated migration/restore, inference, release or SBOM review.

To deliberately refresh dependency inputs (not needed to build):

```sh
uv pip compile pyproject.toml --python-version 3.11 --generate-hashes \
  --no-annotate --no-header --output-file requirements-cpu.lock
uv pip compile build-requirements.in --generate-hashes \
  --no-annotate --no-header --output-file requirements-build.lock
```
