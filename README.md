# Atlas

Standalone headless Atlas, development package 0.4.0.dev10. The canonical corpus is embedded Qdrant; operational state is SQLite. HTTP, Streamable HTTP MCP and a thin CLI share the existing Engine dispatch. Hermes and InvokeAI are optional clients/backend integrations, not startup dependencies.

This is a development package, not completion of the full Atlas product. See [SPEC.md](SPEC.md) and [OPERATIONS.json](OPERATIONS.json) for retained capabilities and exercised scope. No UI is delivered.

## Install and start

Linux and Python 3.11 are the first acceptance target. Use a new environment; do not install into an active service environment.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install /absolute/path/to/atlas
mkdir atlas-run
cd atlas-run
```

Create explicit configuration and a private local credential (standard library only):

```sh
../.venv/bin/python - <<'PY'
import json, os, secrets
from pathlib import Path
os.umask(0o077)
token = secrets.token_urlsafe(36)
Path('token').write_text(token)
Path('clients.json').write_text(json.dumps([
    {'token': token, 'principal': 'operator', 'source': 'local-client'}
]))
Path('atlas.json').write_text(json.dumps({
    'root': 'state', 'clients_file': 'clients.json',
    'host': '127.0.0.1', 'port': 12088, 'connections': []
}, indent=2))
PY
../.venv/bin/atlas --config atlas.json
```

Run requests in another terminal in `atlas-run`:

```sh
../.venv/bin/atlas-client capabilities --token-file token
../.venv/bin/atlas-client status --token-file token
../.venv/bin/atlas-client corpus.put --token-file token --arguments \
  '{"request_id":"example-1","collection":"tags","identity":{"tag":"example"},"expected_revision":0,"body":{"literal":"example"}}'
../.venv/bin/atlas-client corpus.get --token-file token --arguments \
  '{"collection":"tags","identity":{"tag":"example"}}'
```

The CLI's `--endpoint` defaults to `http://127.0.0.1:12088`. HTTP operations are `POST /v1/ops/{operation}` with a JSON object and bearer authentication. Input schemas are `GET /v1/schema`. MCP is Streamable HTTP at `/mcp/`, with the same authentication header; tool names replace operation dots with underscores. In ordinary launcher mode, Host and browser Origin must match the configured loopback listener (or localhost on its port). Explicit container mode accepts only its configured external loopback endpoint; see [container/README.md](container/README.md). Non-browser clients omit Origin. No anonymous endpoint is exposed.

Stop with Ctrl-C/SIGTERM and restart with the same config. Do not open the corpus from another process while the daemon owns it. Request IDs are durable intents: identical retries return the previous receipt; changed input under the same ID conflicts. Attribution metadata is not a separate application ACL.

## Optional integrations

Top-level configuration fields are defined in `atlas_engine/config.py`; unknown fields are rejected. Relative paths are resolved against the config file directory. `--root`, `--host`, `--port` explicitly override the file. No environment override or implicit home configuration is used by the standalone loader.

An optional single connection has this shape:

```json
{
  "id": "local-invoke",
  "backend_generation": "operator-declared-generation-1",
  "account_scope": "operator-declared-account",
  "url": "http://127.0.0.1:9090",
  "token_file": "invoke.token",
  "enable_writes": false
}
```

Put it in `connections` before the first start of a NEW state root. The binding is pinned, including an empty binding; changing it later is refused until a reviewed migration is available. More than one connection is not yet supported. Declaration does not authenticate the backend's identity/version. Native writes default off; do not enable them against a real service without reviewing the retained legacy contracts and obtaining applicable authorization.

`vocabulary` optionally names a compatible tags/related SQLite vocabulary. `reference_url`/`reference_token_file`, `reference_collections`, `reference_mirror`/`reference_delta`, `projection_dir`, `policy_file`, `civitai_url` and `media_config_file` retain existing adapters. Media binding contents currently require explicit worker/model paths. Legacy Windows source/install adapters still need their explicit policy/SSH integration and retain platform-specific paths and remote environment/registry credential lookup; they are not a portable installation acceptance result.

For local semantic encoding, install `atlas-engine[semantic]` from this source and provide compatible local-only weights under `state/cache/fastembed`. Startup never installs or downloads models. Missing vocabulary, InvokeAI, references or media bindings produce explicit unavailable/disabled results while the corpus remains usable. Merely listing an operation does not certify an optional backend or an inference result.

## Optional standalone CPU censor

Set `"media_config_file":"media.json"` in atlas.json. This optional module needs a separately provisioned, trusted NTD11 Python environment, existing model and anime-mosaic renderer; none are bundled or downloaded by Atlas. No Hermes configuration or service is needed. Example operator-owned media.json:

```json
{"censor":{"python":"/opt/ntd11/venv/bin/python","upstream":"/opt/ntd11/anime-mosaic","model":"/opt/ntd11/models/ntd11_anime_nsfw_segm_v5.pt","timeout_seconds":1800,"max_rss_bytes":3221225472},"max_storage_bytes":2147483648}
```

Relative inner paths belong to media.json's directory, not the current working directory. Preserve the Python launcher path (do not resolve a venv symlink to system Python). The exercised external environment uses torch 2.5.1+cpu, ultralytics 8.4.153 and numpy 1.26.4; use an independent environment for dependency changes. Trusted renderer/model source, licenses and provisioning are operator responsibilities. Existing resources are read-only; model weights and upstream source do not belong in this repository. Linux is required for owned-child lifetime binding. No Windows media acceptance is claimed.

Use the same authenticated HTTP operations or MCP tools:

1. `media.upload.begin`: request_id, size, sha256, role=`image`.
2. `media.upload.chunk`: returned upload_id, contiguous offset, base64 (at most 32768 decoded bytes per chunk).
3. `media.upload.finalize`: upload_id; verifies digest and single-image decode.
4. `censor.process`: `{"request_id":"censor-1","images":[{"upload_id":"RETURNED_UPLOAD_ID","name":"safe.png"}],"mode":"mosaic"}`. Returns a durable **queued** receipt, not completed inference.
5. `censor.get`: job_id. Inspect job state/error/result; the operation envelope alone is not success. `jobs.events` is also available.
6. `artifact.list`: job_id; `artifact.read`: artifact_id, offset, size. Decode base64 and check SHA-256 against the artifact receipt. The manifest records input/output digests, dimensions, options, detections and model/renderer hashes.

One shared media slot includes queued/running censor and synchronous mask operations. Another new intent receives CAPACITY; there is no hidden backlog. Exact replay reads the original job even while inference occupies the slot. Polling, events, artifacts and core reads do not take that lock; the existing numerical creative worker remains separate. Normal stop terminates/reaps the owned child; abrupt owner death kills it on Linux. Restart marks queued/running media failed with INTERRUPTED and never silently reruns; use a new request_id only for an explicit new attempt. Public media cancellation is unsupported. Historical completed artifacts/prior manifests remain available. Actual mask acceptance is described below.

Existing byte/pixel/batch/storage limits and CPU thread/RSS/deadline caps remain. Admission reserves 520 MiB of output headroom against the 2 GiB default media budget; cleanup is operator-controlled. Offline flags and disabled dependency autoinstall are not a general OS network sandbox. Back up only after stopping/draining the Engine, using the existing shared-lock offline backup command; restored-root quarantine still blocks dispatch/workers until reconciliation. See [BACKUP.md](BACKUP.md).

```sh
/absolute/atlas-venv/bin/python -I -B verification/media_portable.py \
  --out /new/private/media-evidence \
  --python /absolute/ntd11/venv/bin/python \
  --upstream /absolute/anime-mosaic --model /absolute/ntd11.pt
```

The installed dev8 runner exercised real CPU inference on safe synthetic pixels without detections, original-byte preservation, HTTP/MCP artifact/hash/dimensions and restart/replay. It separately labels its slow-child fixture for latency/capacity/timeout/stop/crash checks. Synthetic no-detection input proves execution, not detector quality or detected-region rendering. No live service, GPU job or model download was used. Source, wheel and installed Python module sets/bytes must match before acceptance. Docker now packages dev10; container media inference still requires an operator-supplied runtime and weights and is not accepted.

## Optional standalone CPU mask

Set `"mask":{"root":"private-mask"}` in media.json. Root must be a NEW private copy of the externally provisioned `atlas_mask` Python package, not a symlink to a shared deployment. Copy package source only, never its runtime/socket/cache or someone else's config. Add an operator-owned `backend-paths.json` with exactly `python`, `source`, `weights` absolute paths to the trusted CPU environment, MobileSAM source and checkpoint. The external package currently requires absolute inner paths; Atlas resolves `mask.root` relative to media.json. Nothing is bundled or downloaded. Use a short local root: Unix socket paths have a platform length limit.

Engine refuses pre-existing sockets and competing root owners. It launches/reaps its own worker, with Linux parent-death binding; the subordinate client cannot spawn replacements. After a hard kill a stale socket may remain: only after verifying that private worker is dead may its operator remove that private socket, or configure a new private root. Never reset/release/clean up a shared worker. Idle expiry is also possible.

After the same verified media upload:

1. `mask.setup`: `{"upload_id":"UPLOAD"}` → cache_id, original width/height, source SHA-256. Stored raster coordinates, no EXIF rotation; alpha is discarded by RGB conversion, not composited.
2. `mask.segment`: `{"request_id":"mask-1","cache_id":"CACHE","points":[{"x":160,"y":130,"label":1},{"x":20,"y":20,"label":0}],"box":[60,40,245,205],"multimask":true,"annotate_prompts":false}`. Returns a **synchronous** job; inspect state. Points/box are original raster pixels. White mask pixels mean foreground, black background; native inpaint import is not accepted here.
3. Choose an explicit candidate index, not an assumed automatic winner. Refine with a new request_id, same cache_id, prompts and `"prior":{"job_id":"FIRST_JOB","candidate":1}`. The result retains public prior reference, exact source-manifest and selected-logits SHA-256. The original subordinate manifest stays immutable for model/image/dimensions/logits validation.
4. `artifact.list` / `artifact.read` expose every candidate PNG, overlay PNG, finite float32 `(1,256,256)` NPY logits and public manifest. Check receipt hash/size; PNG MIME is image/png. SDK MCP uses `mask_setup`, `mask_segment`, `mask_get`, `artifact_list`, `artifact_read` with identical inputs.

Restart does not restore an ephemeral cache: a new segment using it fails STALE_CACHE with setup-again guidance. Completed same-request replay/artifact reads do not load a model. New setup from the original upload permits refinement using the completed prior. Mask stays synchronous: the exercised 320×240 geometry segment took about 0.18 seconds; this is not a latency SLA or async acceptance. Health is explicit and can start a worker, ordinary status does not.

Repeat installed acceptance with an explicitly supplied NEW private backend and outside-source output:

```sh
/absolute/atlas-venv/bin/python -I -B verification/mask_portable.py \
  --out /new/private/mask-evidence --backend /absolute/private-mask
```

The dev9 synthetic object acceptance establishes actual CPU model execution, binary polarity, candidate artifacts, prior refusals, restart/cache expiry and original-byte preservation, not broad segmentation quality, EXIF/alpha image-suite acceptance or native Canvas integration. Weights/dependencies/licensing remain operator responsibilities; Docker packages dev10; container mask inference is not accepted.

## Native workspace prompt subset

The registry currently has 115 operations (counted from the shared input schemas). Listing an operation does not certify its backend; `OPERATIONS.json` records the exercised boundary.

`projects.get` and `projects.savePrompt` implement a narrow InvokeAI 7 split-v4 codec: an existing layered workspace (`settingsLayering=1`), canvas compatibility at most v2 and an explicitly selected `generate` widget. A save changes only `widgetInstances[id].state.values.positivePrompt`; opaque content and all other workspace values are preserved. This does not create workspaces, migrate documents, resolve inherited defaults, compile workflows or support arbitrary Canvas editing.

Both operations require `connection_id`, `backend_generation` and `project_id`. Save additionally requires `request_id`, `expected_workspace_revision`, `widget_instance_id` and `prompt`, and the connection's `enable_writes` must be true. Obtain the workspace revision from `projects.get`. A successful operation envelope can contain a failed or `ack_unknown` job receipt: inspect its `state`. Use a NEW request ID only for an explicit new intent, never to bypass an uncertain ACK. Replays and restart recovery never repeat a native PUT. An acknowledged save followed by another workspace edit can remain `ack_unknown`; matching prompt text does not prove authorship.

The committed runner below is a deterministic HTTP contract fixture. A separate isolated integration also exercised the pinned native projects router, original service/DTOs/serialization/transactions/CAS and SQLite schema with a synthetic caller and dependency wiring. It is not full-server/auth/migration or frontend hydration acceptance. No live backend was written. The broader uncertainty/crash scenarios remain fixture-tested.

## Repeat the slice acceptance

From the source directory, using the environment where the package is installed:

```sh
/absolute/venv/bin/python -I -B verification/acceptance.py \
  --wheel /absolute/path/to/atlas_engine-0.4.0.dev10-py3-none-any.whl \
  --out /absolute/new/private-acceptance-directory
```

The output directory must not exist. It holds synthetic data, logs, generated credentials and `result.json`; keep it OUTSIDE the source/release. The Linux runner exercises a nondefault `127.0.0.2` bind, rejects unintended non-loopback child connections and uses no live corpus/backend. It compares installed/source/wheel module sets and bytes, exercises real MCP initialization/list/call, CLI/HTTP replay, auth failures, owner contention and restart. Omitting `--wheel` skips the wheel comparison only.

After that module-equality check, exercise the installed workspace handlers against the HTTP fixture:

```sh
PACKAGE_ROOT=$(/absolute/venv/bin/python -I -c \
  'import atlas_engine; from pathlib import Path; print(Path(atlas_engine.__file__).parent.parent)')
/absolute/venv/bin/python -I -B verification/native_workspace.py \
  --package-root "$PACKAGE_ROOT" --out /absolute/new/private-workspace-directory
```

An explicit `--package-root` selects installed/built modules; without it this fixture runner selects the source tree. Its checks cover HTTP/MCP saves and readback, opaque preservation, independent revisions, stale/concurrent CAS, scoped request conflicts, restart, lost ACK, durable ACK reconciliation and intervening edits. It does not import or start InvokeAI.

## Native queue admission

`generation.submitGraph` accepts `connection_id`, `backend_generation`, `request_id`, a native `graph` and optional `runs` (default 1, maximum 4). Configure a NEW isolated root with a pinned connection, `enable_writes: true`, and a policy file containing `{"accepted_modes":["native_graph"]}`. This explicit policy enables operator-supplied graphs; it does not validate model recipes or authorize GPU work. The original generation/image recipes are retained separately.

The existing generation ledger durably saves scope, graph ID, retry key, batch defaults and complete native receipt. Exact request replay/restart never repeats a fenced enqueue. Lost ACK recovery reads the pinned native receipt endpoint; 404 stays unknown, not permission to submit again. Read jobs with `generation.get`, refresh using `observer.reconcile`, and cancel known receipt IDs with `generation.cancel`. Inspect job state even in a successful operation envelope. Native receipts are acknowledged only after the complete local receipt transaction commits. Lost ACK replies leave maintenance pending; the existing observer safely repeats the scoped ACK, including after native retention removes a receipt. Saved IDs and receipts remain local, never a reason to enqueue again. Native controls retention; Atlas does not purge receipts/history. A disabled native-write setting also disables receipt ACK maintenance.

Repeat queue acceptance only against a disposable loopback backend:

```sh
/absolute/venv/bin/python -I -B verification/native_queue.py \
  --native-url http://127.0.0.1:DISPOSABLE_PORT \
  --package-root "$PACKAGE_ROOT" --out /absolute/new/private-queue-directory
```

This runner submits integer graphs, cancels its exact items and injects lost replies/process crashes. Do not point it at a live service. Its earlier private native harness exercised original router/service/SQLite admission and cancellation with a fixture graph codec. A separate dev7 CPU probe now executes the original Graph validation, integer invocation and processor/runner with synthetic config/caller/services and a Python 3.11 shim. This is partial processor acceptance, not full bootstrap/auth/migrations or GPU/model jobs.

In dev7, HTTP `generation.get` and MCP `generation_get` expose the observed result in `native_items["<item_id>"].native_results.entries`: exact prepared/source node IDs, native type, status, and bounded integer/boolean value. The real no-model graph returns `integer_output` value `17` through Atlas, including after restart, history-missing, exact replay and native shutdown. Values live in the existing ledger, with native session/scope/provenance retained. At most 64 entries per item; unsupported types and omissions are explicit. Strings/opaque DTO fields are not copied. Existing `outputs` image names and the artifact pipeline are unchanged. See [SPEC.md](SPEC.md) for the contract.

```sh
/absolute/venv/bin/python -I -B verification/native_results.py --out /new/private/results-evidence
```

This narrow installed regression uses synthetic DTOs and PNG bytes; it is not image-model execution evidence.

## Callable native saved workflows

`workflows.schema` inspects an existing saved workflow on the pinned native connection. `workflows.run` calls original InvokeAI `call_saved_workflow` and returns an ordinary generation job. Enable native writes and explicitly add `saved_workflow` to policy `accepted_modes`. Atlas does not create or compile saved workflow documents; InvokeAI owns the library and child graph construction.

```json
{
  "connection_id": "local-invoke",
  "backend_generation": "operator-declared-generation-1",
  "workflow_id": "NATIVE_WORKFLOW_ID",
  "request_id": "call-1",
  "version_semantics": "latest_at_execution",
  "workflow_inputs": {"saved_workflow_input::n::value": 17},
  "return_key": "result"
}
```

Pass only the first three reference fields to `workflows.schema`. Its `workflow_inputs` gives exact supported field names; omitted values use native defaults. Literal supplied names and integers are not trimmed/coerced. The observed subset is schema 1.0.0, at most 32 integer/workflow_return_value/workflow_return nodes and 64 edges, exposedFields integer.value inputs (no form), and literal bounded named return keys. Native compatibility must explicitly report callable. Other schemas/nodes/inputs fail explicitly. This subset is intentionally smaller than general native workflow support.

`latest_at_execution` is required acknowledgement: native reloads the workflow by ID when the call executes, including edits after admission. `workflow_observation.observed_revision` and `observed_sha256` describe the preflight GET only; `executed_revision` is unknown/null. No immutable-revision/snapshot execution is offered. A later edit can change outputs, fail validation or introduce nodes outside the observed subset; the mode is not a CPU/GPU authorization sandbox. Do not enable it for a mutable library whose writers you do not trust.

Read/poll with `generation.get` / `generation.wait`; MCP tools are `workflows_schema`, `workflows_run`, `generation_get`. The selected named result appears at `native_items["<root_item_id>"].native_results.entries`, with source_node_id=`call`, type=`workflow_return_output`, return_key=`result`, status=`available`, value=`17`. Only signed-64-bit integers are copied. Missing keys, strings/booleans/objects and out-of-range integers get `return_missing`, `unsupported_value_type` or `value_out_of_range`, without opaque values. Native job success and supported result availability are separate; inspect both. Image outputs/artifacts are unchanged.

Intent binds scope, native ID, literal inputs, return key and version semantics before the single fenced POST. Exact replay reads the saved receipt without preflight/enqueue; changed arguments conflict. Lost enqueue ACK uses existing native receipt recovery, never blind retry. Native controls descendants; Atlas does not adopt child IDs into the root receipt or store another workflow library. A new explicit run needs a new request ID, including after a native failure. Completed results survive Atlas/native restart and native unavailability once observed. Unobserved results lost from native history cannot be reconstructed.

Installed dev10 acceptance exercised authenticated HTTP and SDK MCP integer17, original native child/return execution, restart/replay and lost ACK, plus a real native revision1→2 edit between admission and execution returning integer23. Four root enqueue POSTs produced four roots and exactly four native children. A string return was explicitly unsupported without copying its contents. This uses the pinned original router/storage/Graph/processor/queue on synthetic metadata-created SQLite, synthetic config/caller/services/thumbnail wiring and a Python 3.11 shim. Full InvokeAI bootstrap/auth/migrations, GPU/model execution and arbitrary Canvas/compiler parity remain unaccepted. Private harness/evidence are not release payload; Docker packages dev10; native execution inside the container is not accepted.

## CPU Docker delivery

[Build and isolated bridge run](container/README.md) documents the non-root CPU image, private mounted configuration, writable state volume and repeatable `--docker-image` boundary acceptance. The concise [source/packaged/live outcome matrix](SPEC.md#docker-delivery-contract-and-outcome--dev10) separates this delivery from earlier native/media probes. Its base digest and hashed dependency inputs are pinned independently of the package version ranges. HTTP, real MCP and the installed host CLI were exercised against the built image on clean volumes, including auth negatives, duplicate ownership refusal and restart persistence.

The current CPU Docker image is 0.4.0.dev10, built from current source with the existing pinned base and dependencies. Accepted: complete source/wheel/installed/image Python module equality, real HTTP/SDK MCP/host CLI, auth/Host/Origin negatives, owner refusal, durable corpus restart/replay, and current workflow public schemas with explicit missing-backend refusal. Shipping native/media adapters does not accept their execution inside this image. The installed `atlas-state` executable also completed populated synthetic offline backup→new-root restore inside the non-root read-only image with networking disabled, exact durable bytes/inventory comparison, shared-lock refusal and quarantine/reconciliation checks; see [BACKUP.md](BACKUP.md). Full legacy migration, full native server execution, GPU jobs and container media inference remain unaccepted (installed host censor acceptance is documented above). Source licensing/third-party redistribution, SBOM and release provenance still need release review before publication. No live deployment or public service is part of this delivery.

## Populated literal search and numerical creative discovery

Configure `"vocabulary":"vocabulary.sqlite"` in `atlas.json` before starting the daemon. Supply a closed, immutable, compatible SQLite snapshot, keep it outside source, and work on a private copy. Do not copy a live WAL database as a single file or mutate a running vocabulary. Required tables for this adapter:

```sql
CREATE TABLE tags(id INTEGER PRIMARY KEY, tag TEXT UNIQUE, category TEXT, post_count INTEGER);
CREATE TABLE related(tag TEXT, related_tag TEXT, score REAL, support REAL,
                     pair_count INTEGER, pmi REAL, related_count INTEGER);
CREATE INDEX outgoing ON related(tag);
CREATE INDEX incoming ON related(related_tag);
```

Extra source columns are allowed. IDs must be stable numeric identities; edges retain their observed direction, conditional support, pair counts, PMI and target frequency. Category is descriptive metadata, not a ranking rule. No graph is inferred from prose, knowledge bodies or canonical corpus text. Canonical Qdrant history, the optional numerical vocabulary and historical knowledge are separate stores, not silently merged indexes.

```sh
atlas-client search --token-file token --arguments '{"query":"observatory","mode":"literal"}'
atlas-client enrich --token-file token --arguments '{"tags":["observatory"],"prompt":"  Unknown Ω  ","seed":17,"budget":3}'
atlas-client dream.submit --token-file token --arguments '{"request_id":"idea-1","tags":["observatory"],"seed":17,"budget":3,"count":2,"trajectories":4}'
atlas-client dream.get --token-file token --arguments '{"job_id":"RETURNED_JOB_ID"}'
atlas-client feedback.record --token-file token --arguments '{"request_id":"review-1","target_id":"RETURNED_JOB_ID","reaction":"reject","note":"Prefer a different next input"}'
atlas-client feedback.list --token-file token --arguments '{"job_id":"RETURNED_JOB_ID"}'
```

Use an actual observed tag in your vocabulary instead of the synthetic example `observatory`. `search` returns separate `tags`, `resources`, `knowledge`, `corpus` and `semantic` lists/views. An unconfigured reference backend is explicitly unavailable/partial but no longer prevents local populated lookup. Canonical search reads stored current head revisions, excludes tombstones and superseded bodies, caps scanning at 5000 heads and marks truncation with `corpus.complete:false`. It is not a paginated or semantic index; revision-only interrupted heads remain recoverable through existing `corpus.get/page`. Optional-source auth/protocol errors are not silently treated as absence. Missing vocabulary still fails explicitly; semantic/hybrid encoding is not accepted by this slice.

Enrich is synchronous; Dream is a durable CPU job: inspect `state`, not just the operation envelope. Literal prompts/unknowns and binding scopes remain intact; results expose directed evidence, ancestry, numerical utilities and vocabulary SHA-256. Empty Dream proposes from observed incident IDs, never inferred intent. Unknown-only intent fails instead of becoming a random scene. Same normalized input/seed and unchanged vocabulary/reference heads reproduce results; completed request replay returns the saved job without recomputing. Newly admitted Dream work pins vocabulary bytes; changed vocabulary after interruption yields `VOCABULARY_CHANGED`, not a different result under an old intent. Older jobs lacking that pin retain historical compatibility, not a new retroactive provenance guarantee.

`capabilities.feedback_policy` is explicit: feedback is a durable attributed **job-level annotation only**. It does not train weights, alter graph rankings or create hidden tag constraints. The next identical request produces identical output; the caller explicitly revises the next input. `feedback.list` preserves all reactions and literal notes across restart. Historical knowledge has a separate existing offline importer (`atlas-delta-import`, reviewed manifest/archive and explicit isolated-destination marker); no new network ingestion endpoint is introduced.

Repeat the installed populated outcome with a NEW private output directory:

```sh
/absolute/venv/bin/python -I -B verification/creative_portable.py \
  --wheel /absolute/path/to/atlas_engine-0.4.0.dev10-py3-none-any.whl \
  --out /absolute/new/private-creative-directory
```

Default data is an owned synthetic graph, synthetic canonical revisions and a synthetic knowledge reference, not real-vocabulary or full migration acceptance. The runner checks HTTP lookup/enrich/Dream, SDK MCP replay, graph evidence, category/rename invariance, active numerical/public cancellation, SIGKILL recovery, changed-vocabulary refusal, feedback's annotation-only effect and exact restart/history preservation. `--vocabulary /sealed/snapshot.sqlite --anchor known_literal_tag` copies an explicitly supplied immutable vocabulary before opening it; this path skips synthetic active-crash/cancel injection and is not a claim that real vocabulary was exercised in the shipped receipt. The same runner generates no credentials from Hermes, uses empty HOME and forbids child non-loopback connections. Do not commit its evidence or databases.
