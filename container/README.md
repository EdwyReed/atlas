# Atlas container delivery

Current development package: dev19, not full-product completion. The SSH-only successor IDs and provisioning boundary are below. Earlier dev18 feature evidence remains applicable to unchanged modules; historical dev12/dev10 instructions retain their original evidence only. No production deployment is implied.

## Existing Windows SSH transport — dev19

| Artifact | Immutable local identity |
|---|---|
| `atlas-core:standalone-dev19-ssh1` | `sha256:e821ca4dc3ebcfff8ca0758b49060f23736c4052a0fa5d49786d125e3814a25e` |
| PRIVATE `atlas-core:media-dev19-ssh1` | `sha256:d922ec0fa8e22c0c9e0e4acd64ce0736e773175eb20cfdc1e7fa2490672de5a4` |
| `atlas_engine-0.4.0.dev19-py3-none-any.whl` | SHA256 `c1abfd4842f92414f1027d28d4a860b18019c36730a0fd06278410aa69d46b9f` |

`container/Dockerfile.ssh` is the narrow successor recipe for an explicitly pinned existing core OR media image plus a named `atlas_release` directory containing only the wheel. It adds `openssh-client=1:9.2p1-2+deb12u10` and its system dependency closure, installs the wheel without Python dependency resolution, and copies the agent helper. It does not rebuild heavy semantic/media dependencies. The main Dockerfile also provisions SSH for future clean builds; only the narrow successor images above were exercised in dev19. This is not reproducible-image or general dependency-upgrade acceptance. Original dev18-final14 images remain rollback material.

`container/compose.ssh.yaml` merges into an operator-owned base defining service `atlas`. Bind explicit accepted `ATLAS_IMAGE_ID`, existing single `ATLAS_SSH_IDENTITY_FILE`, existing selected `ATLAS_SSH_KNOWN_HOSTS_FILE`, alias-only `ATLAS_SSH_CONFIG_FILE` and exact `ATLAS_SSH_DESTINATION` (`user@hostname`). Missing inputs refuse. Use `docker compose -f ABS_BASE -f container/compose.ssh.yaml config` for preflight; it does NOT authorize `up`, activation or changing live configuration. Do not use a dev18 `init` helper on an existing config volume.

The private SSH config must bind the existing policy alias to its already trusted target and user, with these options (replace only the alias/target/user placeholders):

```sshconfig
Host EXISTING_ALIAS
    HostName EXISTING_HOSTNAME
    User EXISTING_USER
    IdentityAgent /run/atlas-ssh-agent/socket
    IdentityFile none
    UserKnownHostsFile /run/atlas-ssh/known_hosts
    GlobalKnownHostsFile /dev/null
    StrictHostKeyChecking yes
    UpdateHostKeys no
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    ForwardAgent no
```

Mount the one existing key read-only into the network-disabled sidecar ONLY, never the application, a whole `.ssh` directory or HOME. The namespace-root loader has only the declared mount/UID-management capabilities; the agent runs as UID10001 and accepts only the destination constrained by the existing host pins. The application stays UID10001 with a read-only socket mount and no key material. The sidecar cannot enroll/update host keys or create credentials. Missing/unreadable/encrypted identity, absent pins, stopped agent and unknown host refuse without credential fallback. Coordinate sidecar stop with application stop; Compose readiness orders initial startup, not automatic lifecycle coupling. The socket permits signing to the existing destination, not a per-command remote ACL; retain the operator's existing Windows account permissions and do not treat this as a new grant.

The retained release evidence includes a read-only Windows probe for both exact image IDs, including app/agent restart and stopped-agent/unknown-pin refusal. It records complete package names and hashes across source, build, wheel, isolated install and both images, including sidecar/media-init helper equality. There are no production data mounts in these checks. `verification/ssh_release_parity.py --help` documents the offline repeat command. Generated Python bytecode is excluded from filesystem inventories; every actual wheel package member must match. The isolated install is package-only (`--no-deps`), not a second full dependency acceptance. Documents are updated outside the frozen wheel; wheel METADATA retains the build-time README.

The deployment owner supplies the private provisioning bindings and owns final fencing, refresh, reconciliation and activation. Baseline `UPSTREAM_UNAVAILABLE` is independent of this transport fix. No GPU, installer, native write, service restart, credential copy or cutover was performed by this release finish.

## Retained dev18 feature evidence

Core HTTP/MCP/CLI/auth/nonroot/read-only-rootfs/ownership/restart and retained recovery-data restore into an independent container have been exercised. Optional CPU media image `atlas-core:media-dev18-r6`, immutable ID `sha256:a991fdbf534164294f3ccc61046a0b66fb2dcc9d3950ff190942dbcd06d7e29a`, passed actual MobileSAM + NTD11 CPU inference/artifact/restart checks on synthetic geometry. It predates the newest source-credential wiring; do not assume that tag equals later source. Media inference/detection quality, actual model install/GPU execution and fresh live-state migration are separate acceptance boundaries.

## Optional private CPU media image

The main Dockerfile includes hash-pinned BGE/ONNX CPU dependencies. Semantic weights are separately provisioned under the state volume's `cache/fastembed`; no startup download occurs. The media image uses a separate pinned Python environment because the retained imgutils renderer requires NumPy1 while core semantic uses NumPy2. The CPU censor child denies sockets and subprocess installers, including import-time dependency fallback.

Prepare a NEW private kit OUTSIDE the repository. Inputs must be explicit operator-owned source and weight paths; the helper copies only selected Python/notices and model files, never a venv, credentials or runtime socket. Do not publish third-party source/weights without checking redistribution rights.

```sh
python3 container/media-kit.py --mask-root "$MASK_ROOT" --sam-source "$SAM_SOURCE" \
  --sam-weights "$SAM_WEIGHTS" --censor-source "$CENSOR_SOURCE" \
  --censor-weights "$CENSOR_WEIGHTS" --out "$PRIVATE_KIT"
docker build -t atlas-core:standalone-dev18 .
docker build -f container/Dockerfile.media \
  --build-arg ATLAS_IMAGE=atlas-core:standalone-dev18 \
  --build-context media_sources="$PRIVATE_KIT/sources" -t atlas-core:media-local .
```

Use the ordinary run's NEW owned state/config volumes, but initialize models BEFORE starting the daemon. The configuration must explicitly contain `media_config_file:"/run/atlas/media.json"`; provision does not edit atlas.json. Initializer refuses an existing media kit/config and copies/verifies weights once. Only that one-shot helper receives the input mount; the running daemon receives its two volumes, not host source/env/model directories.

```sh
docker run --rm --network none --user 0:0 \
  -v "$NAME-state:/var/lib/atlas" -v "$NAME-config:/run/atlas" \
  -v "$PRIVATE_KIT/weights:/inputs:ro" --entrypoint python \
  atlas-core:media-local /opt/atlas-media/init.py
# Start with the ordinary bridge command below, media-local image, 3g/2CPU/256pids.
```

Repeat `verification/media_docker.py --image atlas-core:media-local --weights "$PRIVATE_KIT/weights" --out NEW_PRIVATE_EVIDENCE` with the matching installed host wheel. The runner exercises uploads, mask candidates, selected-logit refinement, real censor, artifact hashes, source-free daemon mounts and exact restart receipts, then removes its owned resources. Explicit selected-region geometry is checked, not general detector quality.

`container/Dockerfile.refresh` supports a package-only update of an already pinned local image using a named `atlas_release` context containing exactly the built wheel. Use the main Dockerfiles instead when dependencies change. Verify complete module parity before claiming the resulting image matches source. Disk-constrained work should not repeatedly rebuild/retain giant dependency layers.

### Portable media recovery

Stop the owned daemon before packing (both existing storage locks are required). Explicitly select the private weights to include; configuration, secrets and environments are never collected. This is an operator action, not an automatic restart replay:

```sh
atlas-portable pack SOURCE PRIVATE_ARCHIVE --media-weights PRIVATE_WEIGHTS
atlas-portable unpack PRIVATE_ARCHIVE NEW_ROOT
# Prepare and apply the destination/ledger-bound activation report as documented
# by atlas-portable activate --help; retain all historical effects inert.
atlas-portable activate NEW_ROOT PRIVATE_REPORT
```

Inside the matching private media image, mount ONLY the restored state volume and a NEW config volume. After generating ordinary atlas.json/clients.json, set `root` to the exact restored root and `media_config_file` to `/run/atlas/media.json`. Run the one-shot initializer with network disabled:

```sh
docker run --rm --network none --user 0:0 \
  -v "$RESTORED_STATE:/var/lib/atlas" -v "$NEW_CONFIG:/run/atlas" \
  --entrypoint python atlas-core:media-local /opt/atlas-media/init.py \
  --restored --state-root /var/lib/atlas/NEW_ROOT
```

No `/inputs` or original source/model mount is used for restored provisioning. Both archived model hashes and the completed restore binding are checked under the existing offline locks. An existing kit/config refuses rather than overwriting; interrupted provisioning remains visibly incomplete. Start the nonroot daemon with its ordinary two volumes. Completed historical effects remain inert after explicit activation; fresh mask setup/admission is required for new inference. Repacking retains archived weights, not transient model runtimes. The image still supplies the explicit private source/dependency layer; transferring weights alone does not license or install third-party source.

`verification/media_docker.py --portable` exercised populated pack -> new volume/root -> explicit activation/provision -> exact old receipts/artifacts -> fresh real MobileSAM inference -> restart. The source-free successor has only its own state/config mounts. Synthetic geometry is not general detector-quality acceptance. A nested-root regression additionally protects new artifact paths from being remapped as historical ones.

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
  --wheel /absolute/path/to/atlas_engine-0.4.0.dev10-py3-none-any.whl \
  --out /absolute/new/private-container-acceptance-directory
```

Evidence includes `result.json`, `container.json` (image ID, UID, module hashes,
exact create command and port bindings), `container.log`, and `cleanup.json`.
The daemon uses the image's installed `atlas` entrypoint, not source mounts. No
external backend is configured or invoked; unlike self-host mode, Docker mode does
not install a Python child egress audit hook. The ordinary non-Docker acceptance
continues to exercise that guard. Current workflow public schemas and missing-backend refusal are also checked; no native workflow executes. This is core corpus acceptance, not a full legacy
operation/backend, populated migration/restore, inference, release or SBOM review.

## Populated offline backup/restore in the installed image

The dev10 CPU image also passed the existing populated synthetic runner with
`--cli`: the installed `atlas-state` executable performs the primary backup and
restore, including refusal while the shared service/corpus locks are held. The
same runner checks raw five-collection preimages, WAL/ledger/upload BLOBs,
artifact/media bytes, interrupted restores, quarantine and bound reconciliation.
No backend is supplied; network access is refused by the runner and Docker uses
`--network none`. This is same-format synthetic restore, not legacy migration.

From the repository root, choose NEW names and an existing private `RUN_DIR`
outside source. Save the result before removing the exact owned resources:

```sh
NAME=atlas-offline-$(python3 -c 'import uuid;print(uuid.uuid4().hex[:12])')
docker volume create "$NAME-state"
docker create --name "$NAME" --network none \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --cpus 1 --pids-limit 128 \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  -v "$NAME-state:/var/lib/atlas" -v "$PWD:/reference:ro" \
  --entrypoint python atlas-core:cpu-slice \
  -I -B /reference/verification/backup_restore.py \
  --source-root /reference/atlas_engine --cli \
  --out /var/lib/atlas/backup-check
docker start -a "$NAME"
docker inspect --format '{{.State.ExitCode}}' "$NAME" # must be 0
docker cp "$NAME:/var/lib/atlas/backup-check" "$RUN_DIR/offline"
docker rm "$NAME"
docker volume rm "$NAME-state"
```

The source mount is read-only verification/reference input, not a source-package
import or daemon entrypoint. `receipt.json`, `cli.jsonl` and
`source-comparison.json` record checks, executable commands and source preservation.
Separate CLI SQLite readers may change transient `registry.sqlite3-shm` read marks;
all durable source bytes, including the WAL, and logical ledger must remain exact.
Use `docker rm -f "$NAME"` and remove only its exact volume on a failed run too.

To deliberately refresh dependency inputs (not needed to build):

```sh
uv pip compile pyproject.toml --python-version 3.11 --generate-hashes \
  --no-annotate --no-header --output-file requirements-cpu.lock
uv pip compile build-requirements.in --generate-hashes \
  --no-annotate --no-header --output-file requirements-build.lock
```
