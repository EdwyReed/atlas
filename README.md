# Atlas

Standalone headless Atlas, unpublished development candidate 0.4.0.dev19. The canonical corpus is embedded Qdrant; operational state is SQLite. HTTP, Streamable HTTP MCP and a thin CLI share the existing Engine dispatch. Hermes and InvokeAI are optional clients/backend integrations, not startup dependencies.

This is a development package, not completion of the full Atlas product. The current delivery boundary is Docker; [container/README.md](container/README.md) contains startup/provisioning commands. No UI is delivered. Versioned sections below preserve earlier acceptance; they are historical, not the current status matrix. Public schemas (`GET /v1/schema`) describe the actual operations; the older OPERATIONS.json is not a current acceptance claim.

## dev19 SSH compatibility successor

The narrow dev19 successor adds pinned OpenSSH to both dev18-final14 images and an optional `ATLAS_SSH_CONFIG` absolute-file binding shared by the existing Windows metadata and installer adapters. It preserves strict host-key checks, batch-only authentication and no agent forwarding. Without the environment binding, the host adapter retains its existing SSH configuration lookup. This does not select or enable an installer policy, change native/source credentials, enroll host keys, or certify a model install.

Both successor images passed the same existing Windows read-only Python platform/version probe as UID10001, app restart and agent restart. A stopped agent and absent host pin fail closed. Full source/build/wheel/isolated-install/BOTH-image package names and bytes match; see [container instructions](container/README.md#existing-windows-ssh-transport--dev19) for immutable IDs and the constrained sidecar. The earlier dev18 feature evidence below is retained, not rerun or promoted to full-product acceptance. No production cutover occurred.

## Current standalone outcome — dev18

* Core Docker HTTP/MCP/CLI, authentication, nonroot/read-only rootfs, restart, durable replay and exclusive ownership have been exercised. Optional CPU media now executes MobileSAM masks and NTD11 censor in a private dependency-pinned image, with two owned volumes and no host source/runtime mounts. Synthetic geometry proves execution and foreground/background contracts, **not general detector quality**.
* `atlas-portable pack SOURCE ARCHIVE --attachment EXACT_ATTACHMENT --vocabulary CLOSED_VOCABULARY` captures an offline populated root plus its declared historical dependencies. `atlas-portable unpack ARCHIVE NEW_ROOT` restores into quarantine without rewriting ledger/history/provenance. `-` streams stdout/stdin; secrets/configuration are not bundled. A full retained recovery corpus (2215 files) has been restored and read through an independent Docker root, HTTP/MCP/CLI and restart. This is **not a fresh active-service snapshot**. Existing owner locks, complete file hashes, ledger equality and incomplete-root refusal are enforced. `atlas-state` retains its narrower same-format boundary and still refuses adopted roots.
* Explicit `atlas-portable activate ROOT REPORT` enables new unrelated work after a destination/ledger-bound offline operator report. It does not reconcile uncertain historical effects or authorize a backend operation. All original effect rows remain inert; do not replace this with a blanket replay. Use `atlas-portable activate --help` for the report contract.
* Canonical semantic/hybrid search uses local BGE CPU weights in `state/cache/fastembed`, not unverified legacy embeddings. The in-memory Qdrant index is derived from all current stored heads; batched content-addressed vector caches are disposable and checked before reuse. Revision changes invalidate the index. Missing local runtime/weights returns an explicit error; no automatic model downloads or silent literal fallback. Cold indexing cost grows with corpus size.
* Unified `models.acquire.enqueue` supports an explicit `installer:"native"` for InvokeAI HTTP installation of LoRA/checkpoint files, exact selection/hash/native-key verification and the same full onboarding collector. Omitted installer preserves the legacy Windows adapter. Synthetic HTTP native-install + public HTTP/MCP lifecycle/restart/lost-ACK checks pass; **real installation has not been authorized or accepted**. A lost install ACK is unknown, never repaired by matching inventory or repeating POST.
* Registered-file onboarding defaults to local credential-free HTTPS against `civitai.red`; an explicitly configured legacy `windows_lora_installer` policy retains its SSH metadata transport. For authenticated source metadata, configure `civitai_token_file` as a private mounted file. It is not an InvokeAI token. No source bearer/cookies are sent to gallery image requests or redirects, and echoed credentials are refused. No civitai.com fallback. Real anonymous checkpoint collection obtained30 community examples but author RPC returned401: full author coverage is **not accepted**; the successful job honestly remains partial. Source credentials must be supplied by the operator, not copied from another profile or Windows registry.
* Generation plans include explicit `refinement` for SD1/Anima, retaining exact original upload lineage and image dimensions without a fabricated mask. `output_upscaler` optionally composes txt2img/inpaint/refinement followed by an existing registered learned upscaler in ONE native graph; only the final pixels are published/boarded. Txt2img/inpaint/learned upscale/refinement GPU execution remains unaccepted pending separately authorized bounded jobs. Callable saved workflows retain the accepted integer/nearest-resize subset; concrete generation templates are not an arbitrary Canvas compiler.

Canonical semantic scale passed on5103 synthetic heads using the real local encoder, including restart and revision invalidation. Separate checks passed corrupt/deleted/unwritable cache recovery and model-file fingerprint namespace changes; scores and corpus snapshot remained stable. This is not numerical-vocabulary or historical-vector compatibility acceptance.

Portable optional media now supports explicit `--media-weights PRIVATE_WEIGHTS` during pack. Only the two selected pinned weight files enter the private archive, not configuration, credentials, Python environments or runtime caches. Restore into a new volume/root and explicitly provision with the matching private media image's `init.py --restored --state-root ROOT`; historical receipts/artifacts and fresh real CPU mask inference after relocation/restart have passed. See container instructions for exact commands. A core-only image cannot execute media merely because weights were restored.

Remaining full-product acceptance: current live-data snapshot/activation, authenticated real-source dual onboarding and actual install boundary, retained real numerical vocabulary, and authorized GPU/artifact/visual checks including the concrete combined recipes. No sealed compatible real vocabulary was found in the retained creative evidence: an operator-provided closed snapshot is needed; synthetic numeric acceptance is not relabelled real. A passing core/media image is not full-product completion. Source, built wheel, image and live service are separate states; source edits never update a running installation automatically.

### Concrete generation recipes

Use the same body with HTTP `generation.plan`/`generation.submit`, MCP `generation_plan`/`generation_submit`, or CLI `atlas-client generation.plan --arguments JSON`. Resolve exact native model keys first; input images/masks must come from the existing upload/import contract. These are reusable fixed graph templates, not saved-library registration or a new orchestrator.

```json
{"model":"EXACT_CHECKPOINT_KEY","mode":"refinement","image_name":"OWNED_NATIVE_IMAGE.png","prompt":"literal prompt","width":512,"height":512,"strength":0.25,"steps":8,"cfg":1,"seed":17,"scheduler":"euler","output_upscaler":"EXACT_REGISTERED_UPSCALER_KEY","scale":2}
```

For inpaint, use `mode:"inpaint"` and add the owned normalized `mask_name`; for txt2img omit image/mask and use `mode:"txt2img"`. Omit `output_upscaler` for diffusion only; pixel-only upscale remains `mode:"upscale"` with the upscaler as `model` and no prompt/LoRAs. Combined templates use native Spandrel autoscale, tile512 and native multiple-of-eight fit; a non-upscaling model may ignore the scale, so actual dimensions come from verified native output artifacts, not a fabricated guarantee. Submission additionally requires a new stable `request_id`, configured write-enabled connection and the existing mode authorization; planning does not grant permission or run the graph.

## Canonical dev20 adoption — dev16

`atlas-adopt plan SOURCE PRIVATE_PLAN --source-python ABS_DEV20_PYTHON --external PRIVATE_CONFIG --attachment ATTACHMENT --native-binding PRIVATE_BINDING` prepares a private checksum-bound plan. Repeat `--external` for existing configuration, credential, vocabulary and launcher references; never place their contents in Git. Only installed dev20 / Qdrant1.20.0 is supported. The historical reference lineage, seals, source archives and declared hashes are checked without seeding a runtime or fetching remote data.

After separately authorized stopped-owner fencing, use `atlas-adopt capture PRIVATE_PLAN NEW_ROOT --plan-sha256 APPROVED_SHA256`, then `atlas-adopt verify NEW_ROOT` **before the first Engine open**. This makes one quarantined copy, preserves committed registry WAL through SQLite backup, every table/schema/row and exact corpus/CAS/artifact/media bytes. Existing destination, live owners, dependency/schema/runtime drift refuse. Interrupted copies remain incomplete and cannot start. The explicit original native binding preserves unscoped history; native/account identity is an operator assertion, not remotely authenticated evidence.

Installed `verification/adoption.py` exercises actual dev20 synthetic state using production schema metadata, old HTTP plugin and stdio MCP clients, restart/replay, both locks, interruption, checksum and backup refusals, with no workers or outbound I/O. This is fixture acceptance, **not production capture, production readback or full migration acceptance**. Capture performs repeated full-tree/dependency hashing and fsync inside its fence; it has no built-in five-minute deadline. Operators need a measured budget and independent abort/OLD-restart watchdog before requesting a bounded stop window.

Remaining portability gap: ordinary `atlas-state backup` deliberately **refuses adopted roots** rather than dropping their external attachment dependencies and receipt. There is no accepted self-contained adopted-root backup/restore/repackaging path. Unknown external effects remain unresolved; an attestation cannot activate nonterminal native jobs. Production stop/copy/cutover and GPU execution are not authorized by this command or these fixture results. Docker acceptance remains dev12.

## Install and start

dev11 added an explicit offline historical attachment → new quarantined root path, preserving the entire operational ledger and exposing complete knowledge history and raw/literal historical mirror reads. The original mirror remains a pinned external read-only dependency; zero legacy points are relabelled canonical. Full migration/self-contained portability and atomic upstream capture are not claimed. See [ATTACHMENT.md](ATTACHMENT.md). This dev15 slice does not rebuild/deploy Docker.

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

Each optional connection has this shape:

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

Put entries in `connections` before the first start of a NEW state root. IDs must be unique. The ID, URL, backend generation, operator account scope and logical token-file reference as written in the configuration become immutable in `state/connection-bindings.json`. Token contents can rotate at the same private file reference; Atlas does not disclose references or tokens through capabilities. Declaration does not authenticate the backend's identity/version. Native writes default off; obtain applicable authorization before enabling a real service.

### Two connections, one daemon

Merge this fragment into the configuration above (use your own native token files, mode0600):

```json
{
  "connections": [
    {"id":"studio-a","backend_generation":"a-generation-1","account_scope":"a-native-user","url":"http://127.0.0.1:9090","token_file":"invoke-a.token","enable_writes":false},
    {"id":"studio-b","backend_generation":"b-generation-1","account_scope":"b-native-user","url":"http://127.0.0.1:9091","token_file":"invoke-b.token","enable_writes":false}
  ]
}
```

Use the intended binding explicitly, including for native IDs that happen to have the same spelling on both servers:

```sh
../.venv/bin/atlas-client catalog.list --token-file token --arguments \
  '{"connection_id":"studio-a","backend_generation":"a-generation-1"}'
../.venv/bin/atlas-client queue.status --token-file token --arguments \
  '{"connection_id":"studio-b","backend_generation":"b-generation-1"}'
../.venv/bin/atlas-client projects.get --token-file token --arguments \
  '{"connection_id":"studio-a","backend_generation":"a-generation-1","project_id":"my-project"}'
```

The identical JSON goes to HTTP `/v1/ops/catalog.list`, or MCP tool `catalog_list`. Native graph, saved workflow, image/board/import and workspace mutations retain mandatory `connection_id`/`backend_generation` contracts. For an owned no-model server with `enable_writes:true` and policy `accepted_modes:["native_graph"]`, an explicit CPU request is:

```json
{"connection_id":"studio-a","backend_generation":"a-generation-1","request_id":"integer-1","graph":{"id":"integer-graph","nodes":{"n":{"id":"n","type":"integer","value":17,"is_intermediate":true,"use_cache":false}},"edges":[]},"runs":1}
```

Submit to `generation.submitGraph` / `generation_submitGraph`; read the returned Atlas job using `generation.get` and its `job_id`. Native item ID alone is never an Atlas job identity. `generation.cancel/retry`, remote `artifact.fetch` and onboarding resume/refresh/recover resolve the persisted job scope, not a default. Local receipts, artifact list/read and already cached fetch remain available with the native service offline. A new remote fetch never falls back to another server. Each native backend still owns its queue; Atlas retains one SQLite ledger and one Qdrant owner, not one database or subprocess per connection.

Unscoped legacy NEW commands use a sole configured connection. With multiple, they refuse `CONNECTION_REQUIRED` unless the operator sets BOTH `compatibility_routing:true` and `default_connection_id:"studio-a"`. This compatibility setting cannot make mandatory scoped schemas optional. New admitted legacy intents store the selected scope; changing the default never reassigns historical intents. Shared `principal+request_id` remains global: the same key with a different backend conflicts before a second remote write. `status.connections` reports per-backend observer/event health.

Existing external Windows LoRA/checkpoint installers and acquisition enqueue/select/resume explicitly return `MULTI_CONNECTION_UNSUPPORTED` with multiple bindings; their background recovery is disabled in that mode. Native HTTP model jobs/install/scan/catalog and registered-file onboarding use their selected connection, but real model installation/source/GPU acceptance is still absent. No application grants/roles are added.

### Existing roots and binding migration

Binding publication requires the daemon's exclusive `service.lock` and refuses an active corpus owner. Stop every owner before migration; editing configuration does not authorize changing persisted history. Config-relative token references retain the same logical spelling after relocating the config/private files. For an earlier unpublished v2 manifest that pinned an absolute resolved token path, explicitly use `migrate_connection_binding:true` at the original credential location first. Only an exact resolved-location match with unchanged ID/URL/account/backend permits conversion; `token_reference_migrations` records old and logical references. A different token reference, even with identical secret bytes, is not token rotation and refuses startup.

Stop Atlas before editing config; keep the existing root and all state. A root with legacy `connection-binding.json` requires one explicit startup with `migrate_connection_binding:true` and the exact original ID/URL/account/backend binding. The new atomic fsynced v2 manifest IS the migration receipt; it keeps the original binding and `legacy_connection_id`. Historical unscoped jobs/index entries use only that original binding. The old file, jobs, corpus, history and artifacts are not replaced or reimported. The legacy file did not record a token reference: this explicit migration pins the configured reference going forward.

To add a connection, retain every existing entry unchanged, append the new entry, and set `migrate_connection_binding:true` for that startup. The manifest records the addition atomically and fsyncs it. Remove that flag after migration. Changing/removing an existing ID/URL/account/backend/token reference refuses startup before native requests, even with the flag. Do not edit the manifest to bypass refusal. Backup/restore includes the v2 binding manifest; existing restored-state reconciliation/quarantine rules still apply. A removed binding is a startup configuration error, whereas an offline bound server does not prevent local receipt/artifact reads.

`vocabulary` optionally names a compatible tags/related SQLite vocabulary. `reference_url`/`reference_token_file`, `reference_collections`, `reference_mirror`/`reference_delta`, `projection_dir`, `policy_file`, `civitai_url` and `media_config_file` retain existing adapters. Media binding contents currently require explicit worker/model paths. Legacy Windows source/install adapters still need their explicit policy/SSH integration and retain platform-specific paths and remote environment/registry credential lookup; they are not a portable installation acceptance result.

For local semantic encoding, install `atlas-engine[semantic]` from this source and provide compatible local-only weights under `state/cache/fastembed`. Startup never installs or downloads models. Missing vocabulary, InvokeAI, references or media bindings produce explicit unavailable/disabled results while the corpus remains usable. Merely listing an operation does not certify an optional backend or an inference result.

## Registered-model ingestion — dev13

`models.onboard` reuses an already registered LoRA/checkpoint; it does **not** install or load weights. Supply `request_id`, exact `registered_key` and `registered_path`, exact source `model_id`, `version_id`, `file_id`, `file_name`, typed `hash_algorithm` (`SHA256` or `BLAKE3`) and literal `file_hash`. An untyped native hash is not evidence of either algorithm. `gallery_limit` defaults to 30 unique community examples; the entire exact-version author carousel is independent and has no numerical quota. Overlap is stored once with both memberships.

Use `models.onboard.get` with `job_id` for progress; `models.onboard.resume` continues the same interrupted intent. `models.onboard.refresh` requires the prior successful `job_id` and a new `request_id`, retaining historical source observations. Explicit revision-conflict recovery uses `models.onboard.recover`, not a blind overwrite. Inspect returned job state/partial/error: an HTTP success does not certify complete source coverage. MCP names replace dots with underscores. `corpus.files.read` pages saved bytes by SHA256, `offset` and `size` (maximum32768), with base64, `next_offset` and `eof`; verify hash/size. Canonical `corpus.get/page/history` expose the resulting model/generation/tag/relation records separately from operational receipts.

`civitai_url` defaults to `https://civitai.red`. Only that public source or an explicitly owned loopback fixture is supported; no civitai.com fallback. Each dev13 intent pins its source origin: changing it refuses resume, refresh and exact replay with `SOURCE_BINDING_CHANGED` before source requests; historical get remains readable. Pre-dev13 intents without a binding retain the original public-source default.

Installed acceptance uses synthetic source and explicitly synthetic typed native inventory for BOTH model kinds: 30 community +35 author −5 overlap =60 generations each, literal raw metadata, exact tags/relations, all URL-byte HTTP/MCP readbacks, refresh/replay/restart and interrupted/corrupt-file resume. Separate original native catalog/unknown-file registration is not LoRA/main installation/loading acceptance. Neither real external gallery acquisition nor model inference is claimed. Repeat `verification/onboarding_standalone.py --wheel ABS_WHEEL --native-python OWNED_NATIVE_PYTHON --out NEW_PRIVATE_DIRECTORY` using the installed environment's Python with `-I -B`.

Latest accepted Docker is **dev12**, commit `6bce091`, image `sha256:8596598c4b173517b468a0c0430f8f2f5a0b06ba8d69078490980e1db61f3518`. It passed isolated core transport/auth/ownership/restart and populated same-format offline restore. This is not a dev14 container or container-native/media execution acceptance; older dev10 sections below remain historical evidence. No live deployment occurred.

## Native image imports and boards — dev12

Use an explicitly provisioned native connection on a NEW state root, as above, with `enable_writes: true`. This enables native administration independently of GPU generation policy. No model or generation is needed to import bytes. Every import and board mutation requires the exact configured `connection_id`, `backend_generation` and a durable `request_id`. An import also requires explicit `board_id`: a real native board ID or JSON `null` for uncategorized. Upload `project` is attribution only, never an implicit board/project mapping.

Public ingress accepts one PNG/JPEG, at most 32 MiB and 24 Mi pixels, without animation or EXIF rotation. Preserve these existing family rules:

| family / role | Public bytes and native representation |
|---|---|
| `pixel` / `image` | RGB PNG only, without transparency, ICC profile or EXIF rotation; original bytes sent without flattening |
| `sd-1` or `anima` / `image` | Existing PNG/JPEG ingress, including RGBA PNG; original bytes sent unchanged; do not apply pixel-family restrictions |
| `sd-1` or `anima` / `mask` | Grayscale **L PNG**, public white=edit / black=preserve. Explicit `white-edit-to-black-edit-invert-L-v1` sends `255 - value` as native L PNG, without resizing or thresholding |

`pixel` masks are refused. Family labels do not load models or certify inference. Download verification compares native pixels/mode/dimensions to the explicitly submitted representation (inverted for masks), not to original mask identity. GPU inpaint polarity and visual correctness remain unaccepted; existing recipes and policy gates are retained.

Using the authenticated HTTP operation endpoint, CLI or MCP:

1. `upload.begin`: `{"request_id":"ingress-1","size":1234,"sha256":"SHA256_OF_ORIGINAL_BYTES","role":"mask"}`. Optional `project` is attribution.
2. `upload.chunk`: returned `upload_id`, contiguous `offset`, and `base64` of at most 32768 decoded bytes. Exact duplicate chunks are safe; conflicting bytes fail.
3. `upload.finalize`: `{"upload_id":"UPLOAD"}`. Check `state: ready`; finalization verifies declared size/hash and decode.
4. Import with a **separate request ID**, for example:

```sh
atlas-client upload.import --token-file token --arguments \
  '{"request_id":"import-1","connection_id":"local-invoke","backend_generation":"operator-declared-generation-1","upload_id":"UPLOAD","family":"sd-1","board_id":null}'
```

`POST /v1/ops/upload.import` accepts that same JSON object with `Authorization: Bearer …`; MCP `upload_import` takes identical fields. A successful operation envelope is not a completed import: inspect `result.state`. `succeeded` contains `result.image_name`, scoped `reference`, exact native DTO, `family`, `role`, immutable `original` artifact and separate `native_representation`. The latter records transform, submitted `sha256`/size/mode/pixel hash, observed native `download_sha256`/size, original SHA-256 and upload/job/native-image/connection lineage. Top-level `native_bytes_sha256` is the observed **download** hash. Native may re-encode PNG; neither download hash nor submitted mask hash is the original ingress hash.

Read the durable job with `admin.get {"job_id":"JOB"}`. Use `artifact.list {"job_id":"JOB"}` then `artifact.read {"artifact_id":"ARTIFACT","offset":0,"size":32768}` to page the original bytes; decode `base64` through `eof` and verify artifact size/SHA-256/MIME. Native representations are separate receipt metadata, not a second original-byte artifact. `images.get` requires `connection_id`, `backend_generation`, `image_name` and returns current native DTO; it requires native availability, unlike completed local job/artifact reads. MCP names are `admin_get`, `artifact_list`, `artifact_read`, `images_get`.

Create a board using `boards.create {"connection_id":"local-invoke","backend_generation":"operator-declared-generation-1","request_id":"board-1","name":"Owned imports"}`. Inspect its job state and use `result.board.board_id` in import. `boards.get {"board_id":"BOARD"}` returns current board/revision; `boards.images {"board_id":"BOARD"}` lists native names (`"none"` means uncategorized). To move selected images:

```json
{"connection_id":"local-invoke","backend_generation":"operator-declared-generation-1","request_id":"move-1","action":"move","board_id":"DESTINATION_BOARD","image_names":["EXACT_NATIVE_IMAGE_NAME"]}
```

Pass this object to `boards.images.update` / MCP `boards_images_update`. `add` also targets an explicit destination; `remove` requires `board_id:null`. Native membership is authoritative. Board update/delete use an `expected_revision` from `boards.get`; these checks serialize Atlas writes but are GET-before-write, **not remote atomic CAS**. This dev12 full-server acceptance exercises create/import/read/move, not all board administration.

One finalized upload authorizes at most one immutable native import intent. Scope/family/board changes under the same request ID conflict. A new request ID cannot reuse a fenced upload. Exact replay returns the saved job, without another POST. An ACK lost before Atlas knows the native ID stays `ack_unknown` across replay/restart: never match/adopt by bytes, filename or board name and never start a fresh import to conceal uncertainty. A durably saved exact native ACK allows GET-only readback recovery. A failed verification is also unknown, not permission to POST again. Only a verified import becomes available to existing mask recipe consumers. Completed receipts are historical observations; later board moves do not rewrite them.

Repeat only on owned disposable environments (NEW private output, no live target):

```sh
/absolute/atlas-venv/bin/python -I -B verification/native_fullserver.py --images-boards \
  --native-python /absolute/owned-native-venv/bin/python \
  --wheel /absolute/path/to/atlas_engine-0.4.0.dev12-py3-none-any.whl \
  --out /new/private/images-boards-evidence
```

The installed host runner uses the pinned original launcher, services, multiuser auth and fresh migrations. Synthetic RGB/JPEG/RGBA/asymmetric L inputs establish upload/readback/transform/artifact behavior without GPU, models or generation. Lost replies, wrong mask download, process death, restart, exact replay and native-offline local reads are exercised through HTTP and SDK MCP. Retain old dev12 receipts separately when replacing this unpublished candidate wheel. Docker remains dev10; no live deployment or full migration acceptance is implied.

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

The registry currently has 121 operations (counted from the shared input schemas). Listing an operation does not certify its backend; `OPERATIONS.json` records the exercised boundary.

`projects.get` and `projects.savePrompt` implement a narrow InvokeAI 7 split-v4 codec: an existing layered workspace (`settingsLayering=1`), canvas compatibility at most v2 and an explicitly selected `generate` widget. A save changes only `widgetInstances[id].state.values.positivePrompt`; opaque content and all other workspace values are preserved. This does not create workspaces, migrate documents, resolve inherited defaults, compile workflows or support arbitrary Canvas editing.

Both operations require `connection_id`, `backend_generation` and `project_id`. Save additionally requires `request_id`, `expected_workspace_revision`, `widget_instance_id` and `prompt`, and the connection's `enable_writes` must be true. Obtain the workspace revision from `projects.get`. A successful operation envelope can contain a failed or `ack_unknown` job receipt: inspect its `state`. Use a NEW request ID only for an explicit new intent, never to bypass an uncertain ACK. Replays and restart recovery never repeat a native PUT. An acknowledged save followed by another workspace edit can remain `ack_unknown`; matching prompt text does not prove authorship.

Installed dev12 passed `verification/native_fullserver.py --workspace` against the original pinned InvokeAI launcher, strict multiuser auth and fresh migrations 0–34 on Python3.12. The project was created through the original API, which accepts opaque client JSON, with split-v4 content and an existing layered workspace. HTTP and SDK MCP selected-prompt saves were compared with direct authoritative project GET (document=4/canvas=2), including every untouched subtree, another generate widget, prompt history, unknown widget/state/settings and nested opaque values. Reverse native workspace/content edits, independent revisions, stale refusal, an actual native 409 race, concurrent intents, scope/schema/widget/write refusals, persisted binding refusal and completed native-offline receipts passed.

Lost PUT ACK and post-commit Atlas SIGKILL stayed `ack_unknown` across both-daemon restart/replay without another PUT. A durable ACK plus failed verification recovered by GET only; an intervening native edit kept that receipt unknown. The accepted run made exactly 9 Atlas workspace PUTs (8 HTTP200, 1 HTTP409), each with independently observed committed scoped intent before forwarding. Direct test setup/edit traffic added 1 project-create POST, 1 content PUT and 3 workspace PUTs; native queue/models stayed empty. Source/wheel/installed sets and bytes matched (64 Python modules); the reused native package/launcher manifest stayed unchanged (1495 files), all owned processes/proxy stopped. No product gap was found; the matching dev12 wheel is unchanged. Private evidence: `atlas-studio/planning/implementation/native-workspace-fullserver/run-05`.

Repeat with an explicitly owned native runtime and a NEW private output:

```sh
/absolute/atlas-venv/bin/python -I -B verification/native_fullserver.py --workspace \
  --native-python /absolute/owned-native-venv/bin/python \
  --wheel /absolute/path/to/atlas_engine-0.4.0.dev12-py3-none-any.whl \
  --out /new/private/workspace-fullserver-evidence
```

This accepts the narrow API codec, not frontend hydration/UI continuation, broad Canvas/document support or populated legacy migration. The earlier deterministic HTTP fixture and original-component synthetic-wiring evidence remain separate; the fixture also covers crash-before-commit, preflight outage and future-codec refusal. No live backend was written. Docker remains dev10.

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

This runner submits integer graphs, cancels its exact items and injects lost replies/process crashes. Do not point it at a live service. Its earlier private native harness exercised original router/service/SQLite admission and cancellation with a fixture graph codec. The dev7 Graph/integer/processor probe used synthetic config/caller/services and a Python 3.11 shim. Those earlier checks are partial-harness evidence; the later original full-server no-model acceptance is documented below. No GPU/model jobs are accepted.

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

Earlier installed dev10 acceptance exercised authenticated Atlas HTTP and SDK MCP integer17, original native child/return execution, restart/replay and lost ACK, plus a real native revision1→2 edit between admission and execution returning integer23. Four root enqueue POSTs produced four roots and exactly four native children. A string return was explicitly unsupported without copying its contents. That run used the pinned original components on metadata-created SQLite with synthetic config/caller/services/thumbnail wiring and a Python 3.11 shim; edit-race/string-return evidence retains that boundary. The later full-server run below uses original bootstrap/auth/migrations for integer17. GPU/model execution and arbitrary Canvas/compiler parity remain unaccepted. Private harness/evidence are not release payload; Docker packages dev10; native execution inside the container is not accepted.

## Native callable IMAGE resize — dev14

This bounded recipe uses an **existing native saved workflow** with exactly `img_resize` v1.2.2 → `workflow_return_value` → `workflow_return`, nearest resampling, dimensions 1..2048 and an exposed resize.image field (optional width/height). Workflow schema is 1.0.0 with exposedFields, no form. Atlas does not create that document or compile Canvas; the original native callable constructs and executes its child. Provision a NEW pinned connection with native writes enabled and `{"accepted_modes":["saved_workflow"]}` in the policy file. This mode requires a trusted mutable native library; it is not a CPU-only sandbox after edits.

Use the authenticated `POST /v1/ops/{operation}` endpoint or the equivalent CLI/MCP tools:

1. Obtain a native `image_name` on that exact connection, for example through the verified `upload.begin` → `upload.chunk` → `upload.finalize` → `upload.import` flow above. Preserve the import's immutable original artifact. A local path, upload_id or an unscoped ImageField is not a workflow IMAGE reference.
2. Inspect the exact saved workflow:

   ```json
   {"connection_id":"local-invoke","backend_generation":"operator-declared-generation-1","workflow_id":"NATIVE_WORKFLOW_ID"}
   ```

   Pass this to `workflows.schema` (MCP `workflows_schema`). Check `recipe:"native-image-resize-v1"`, `return_type:"image"`, exact `workflow_inputs` names and `observed_return_keys`. The example below assumes resize node ID `resize` and named return `resized`; use the actual observed names.
3. Submit `workflows.run` (MCP `workflows_run`):

   ```json
   {"connection_id":"local-invoke","backend_generation":"operator-declared-generation-1","workflow_id":"NATIVE_WORKFLOW_ID","request_id":"resize-1","version_semantics":"latest_at_execution","workflow_inputs":{"saved_workflow_input::resize::image":{"connection_id":"local-invoke","backend_generation":"operator-declared-generation-1","image_name":"EXACT_NATIVE_IMAGE_NAME"},"saved_workflow_input::resize::width":13,"saved_workflow_input::resize::height":6},"return_key":"resized"}
   ```

   IMAGE must be explicitly supplied with the matching connection/backend. Omitted width/height use native execution-time defaults. A successful envelope is only a job receipt; inspect its `state`.
4. Poll `generation.get {"job_id":"RETURNED_JOB_ID"}` (MCP `generation_get`). Inspect both terminal job state and `native_items["<root_item_id>"].native_results.entries`. Only a verified selected IMAGE has `status:"available"`, a scoped `image` reference and `lineage`; its exact name also appears in `outputs`. Native success alone does not guarantee a supported result. Refusal leaves no final output, with `image_status:"image_lineage_unavailable"` and per-entry refusal status where an entry exists.
5. Fetch that exact selected output using `artifact.fetch {"job_id":"RETURNED_JOB_ID","image_name":"SELECTED_OUTPUT_NAME"}` (MCP `artifact_fetch`). Input/unselected/intermediate names are refused. The receipt includes native bytes SHA-256, size/MIME/dimensions, pixel hash/mode and `native_lineage.input_source` containing the actual executed resize input's connection/backend/name. Submitted IMAGE, root prepared call input and child source/prepared resize input must agree. This is execution lineage, not a pinned library revision or source-byte snapshot.
6. Page `artifact.read {"artifact_id":"RETURNED_ARTIFACT_ID","offset":0,"size":32768}` (MCP `artifact_read`), decode base64, advance by decoded length until `eof`, and verify size/SHA-256 against the fetch receipt. Original import bytes remain a separate immutable artifact. Native storage may re-encode PNG.

Exact request replay is a local receipt read; changed input under the same request ID conflicts. Lost enqueue ACK uses the existing scoped native receipt and GET-only recovery, never a second enqueue. Completed verified results/artifacts are deliberately cached across restart, missing/stale history and native outage. Cold hydration without complete matching lineage refuses promotion; an already verified historical receipt is not invalidated by later missing history.

`workflow_observation.observed_revision/observed_sha256` describe preflight only; `executed_revision` remains null. The original-server run edited native width11→10 after preflight and verified the actual width10 output, without inventing immutable-revision or edited-whole-graph CPU guarantees. Root graph is compared to submitted intent; child nested session/parent references, prepared/source identities and executed recipe are checked. Native exposes no independent child graph ID in parent call history, so the child graph ID is retained from the exact child response, not falsely cross-checked against a nonexistent parent field.

Installed dev14 host acceptance uses original InvokeAI `be318726dc800ff3ba7c6f086fda603fd82abb86`, original launcher/auth/fresh migrations0–34, zero models and owned asymmetric pixels. HTTP and SDK MCP selected returns, exact downloaded native bytes/dimensions/every nearest pixel, both-daemon restart, lost reply/post-commit SIGKILL recovery and mutable defaults passed: exactly5 root enqueues/5 roots/5 children, with5 durable intent-before-forward observations. Matching installed core, native_results and original full-server integer17 regressions passed. A separate cold deep-copy check derives54 refusals from actual saved run-02 responses; it is not another native execution acceptance. Final evidence is `atlas-studio/planning/implementation/native-image-workflows/{run-05,lineage-final,core-02,results-02,integer-02}`. run-03 remains an incomplete attempt: its scratch donor runtime disappeared before restart; run-05 uses a separately reconstructed pinned CPU runtime, not the lost donor's inode continuity.

```sh
/absolute/atlas-venv/bin/python -I -B verification/native_fullserver.py --image-workflows \
  --native-python /absolute/owned-native-venv/bin/python \
  --wheel /absolute/atlas_engine-0.4.0.dev14-py3-none-any.whl --out /new/private/image-run
/absolute/atlas-venv/bin/python -I -B verification/native_image_lineage.py \
  --evidence /private/saved-original-image-run --out /new/private/lineage-run
```

No models/GPU, learned upscale/inpaint, arbitrary compiler, descendant mid-execution recovery, live service changes, container execution or full legacy migration is accepted. Latest accepted Docker remains dev12; dev14 is an unpublished host candidate.

## Original full-server CPU / no-model acceptance

Installed Atlas dev10 was exercised against pinned InvokeAI `be318726dc800ff3ba7c6f086fda603fd82abb86` (7.0.0-beta.1), installed with its CPU dependencies on CPython 3.12.14. The original `invokeai-web --root` launcher initialized a fresh private root, native services, strict-password multiuser authentication and migrations 0–34. No service/caller/config overrides, synthetic migration tables or custom invocations were used. Native runs headless without a frontend build.

| Flow | Accepted host result |
|---|---|
| Setup / login / authorization | Original setup/login/me; missing/wrong bearer, wrong password and repeat setup refused; identity survives restart |
| Explicit native graph | Atlas HTTP submitGraph → original Graph/registry/queue/processor → integer17 via HTTP and real SDK MCP |
| Saved callable | Original authenticated workflow create/readback → Atlas HTTP/MCP schema → SDK MCP run → original native child → selected integer17 |
| Events / recovery | Authenticated Socket.IO polling subscribed and delivered queue hints; authoritative GET hydration, both-daemon restart, exact HTTP/MCP replay and dropped enqueue ACK recovery passed |
| Durable evidence | Exactly 3 root POSTs/3 roots/2 children, all completed under the authenticated user; zero models; SQLite integrity passed; stale/missing history and native-offline local reads preserve results |

The corrected verification proxy handles Engine.IO as text rather than JSON. No product fix or version bump was needed. Complete Atlas source/wheel/installed module equality, installed core transport/auth/ownership/restart acceptance and the compact native-result/image-artifact regression also passed.

Repeat only with separately provisioned, explicitly owned environments and a NEW private output outside source:

```sh
/absolute/atlas-venv/bin/python -I -B verification/native_fullserver.py \
  --native-python /absolute/owned-native-venv/bin/python \
  --wheel /absolute/path/to/atlas_engine-0.4.0.dev10-py3-none-any.whl \
  --out /new/private/fullserver-evidence
```

The runner creates credentials and an isolated loopback server/root, submits only its no-model work, injects faults through a local proxy and reaps its own children. Keep its credentials, logs, databases and third-party runtime/source outside releases. Offline model flags are not an OS network sandbox. This does not accept populated legacy upgrades, interrupted descendant recovery, arbitrary workflows, GPU/image generation, UI or live deployment. The later `--workspace` acceptance is documented above. Docker remains the separately accepted dev10 core/schema/backend-refusal and synthetic offline restore delivery, not native/media execution.

## CPU Docker delivery

[Build and isolated bridge run](container/README.md) documents the non-root CPU image, private mounted configuration, writable state volume and repeatable `--docker-image` boundary acceptance. The concise [source/packaged/live outcome matrix](SPEC.md#docker-delivery-contract-and-outcome--dev10) separates this delivery from earlier native/media probes. Its base digest and hashed dependency inputs are pinned independently of the package version ranges. HTTP, real MCP and the installed host CLI were exercised against the built image on clean volumes, including auth negatives, duplicate ownership refusal and restart persistence.

The latest accepted CPU Docker image is 0.4.0.dev12 (`atlas-core:cpu-dev12`, immutable ID and source commit in `container/README.md`); dev15 has not been built/run in Docker. The earlier dev10 run used the existing pinned base and dependencies. Accepted in that earlier run: complete source/wheel/installed/image Python module equality, real HTTP/SDK MCP/host CLI, auth/Host/Origin negatives, owner refusal, durable corpus restart/replay, and current workflow public schemas with explicit missing-backend refusal. Shipping native/media adapters does not accept their execution inside this image. The installed `atlas-state` executable also completed populated synthetic offline backup→new-root restore inside the non-root read-only image with networking disabled, exact durable bytes/inventory comparison, shared-lock refusal and quarantine/reconciliation checks; see [BACKUP.md](BACKUP.md). Full legacy migration, native server execution inside the container, GPU jobs and container media inference remain unaccepted (installed host native/censor/mask acceptance is documented above). Source licensing/third-party redistribution, SBOM and release provenance still need release review before publication. No live deployment or public service is part of this delivery.

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
