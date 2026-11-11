# Atlas — headless slice 1

## Contract

Install the Python package into a new environment, run `atlas --config atlas.json`, and use a private loopback daemon without Hermes, InvokeAI, a vocabulary, model weights or existing data. One Engine owns the existing dispatch, SQLite ledger, canonical embedded Qdrant and artifact files. CLI is an HTTP client; MCP uses the existing SDK and the same dispatch.

This slice must demonstrate authenticated HTTP status/capabilities/input schemas, a literal corpus mutation with readback, CLI/MCP replay of that exact intent, matching validation, restart preservation and refusal of another storage owner. Missing/wrong credentials and hostile Host/Origin must fail before dispatch. Missing optional backends report unavailable, not synthetic success or a private fallback.

## Implementation boundary

- Package `atlas-engine` 0.4.0.dev3; executable `atlas` (alias `atlas-engine`) and `atlas-client`.
- Configuration is explicit JSON, paths relative to its directory. Defaults → config → explicit `--root/--host/--port`. No environment configuration, `.env` discovery or Hermes imports. Secret references are private files. No embedded plaintext InvokeAI token field.
- Loopback IP bind by default; explicit container mode binds 0.0.0.0 only with an exact `container_endpoint` of `http://127.0.0.1:HOST_PORT`. Container Host/Origin allowlists contain only that endpoint; Docker publication MUST bind host 127.0.0.1. One process; no Uvicorn workers or reload. Every HTTP path, including MCP and unknown artifact paths, passes the same bearer/Host/Origin gate. Old public UI is not mounted or shipped.
- Corpus is `root/corpus`; ledger/artifacts/cache belong to `root`. Existing lifetime locks and corpus revision CAS are reused. This is not distributed Qdrant CAS.
- `connections` supports zero or one InvokeAI connection. More are rejected rather than silently choosing one. ID, URL, operator-declared backend generation/account scope are pinned to the state root; changing even an empty binding requires a new root or a future reviewed migration. Token rotation does not prove backend identity. Multi-connection scoped intents/refs are NOT implemented.
- Existing optional vocabulary, reference mirrors/typed references/deltas, local projection, policy and media bindings remain configurable. These paths were retained, not accepted by the empty-core smoke. Sealed attachment preparation, HTTP projection and migration utilities remain source mechanisms; the new ordinary launcher does not expose every old migration flag.
- `fastembed` is the `semantic` extra, loaded lazily with local-only weights. Missing vocabulary fails explicitly. Missing InvokeAI/reference adapters cannot open a network socket. Public acquisition endpoints remain available for explicit operations; no global production egress ban is claimed.
- Input schemas are inherited, with scoped native workspace schemas added. New output/error schemas, DTO sanitization across all legacy operations, general native CAS/receipts and the complete cycle-03 envelope are not delivered here.

## Retained product inventory

`OPERATIONS.json` is the complete inherited operation-name inventory with slice acceptance statuses. All original backend Python modules are retained; no data, Git history, browser/profile state, evidence, weights or UI assets were imported. Baseline origin/hashes and dirty-neighbor comparison are private planning evidence, outside this repository.

| Family | Slice status / remaining work |
|---|---|
| HTTP/MCP/CLI registry | Implemented; only named acceptance scenarios exercised, not whole transport parity |
| Canonical corpus CRUD/history/import/relations | Retained; put/get/page/history and replay/restart exercised; full fidelity migration remains |
| SQLite jobs/events/idempotency/legacy bindings | Retained; corpus intent replay/conflict exercised; general interruption/recovery not reaccepted |
| Literal/semantic/reference search, knowledge, records | Retained; optional source readiness explicit; populated search/embedding execution not run |
| Enrich/dream/feedback without generative LLM | Retained; missing-vocabulary refusal exercised; creative algorithm scenarios not run |
| Model catalog/install/acquisition/LoRA+checkpoint onboarding/gallery | Retained; no external acquisition/install/registration run |
| Generation/batch/queue/observation/cancel | Scoped durable admission and GET-receipt recovery implemented on existing generation path; isolated native queue admission/cancel exercised with fixture graph codec. No native graph execution or GPU family acceptance |
| Boards/images/uploads/artifact fetch/publish | Retained; no native write or artifact transfer run |
| Inpaint/upscale/refinement and concrete graphs | Existing recipe mechanisms retained; GPU/mask polarity/lineage acceptance pending; refinement coverage not established |
| Mask/censor/media workers | Retained and configurable, off without bindings; packaged CPU inference/early async admission/polling acceptance pending |
| Native projects/workspace CAS/codecs and callable saved workflows | Narrow split-v4 generate-widget prompt codec implemented and fixture-tested; isolated pinned native router/service/SQLite smoke exercised. Full native server/auth, other codecs and callable saved workflows not accepted |
| Full Canvas/compiler and native image-map semantic search | Not delivered; concrete versioned subsets/negotiated adapters remain target work |
| Backup/restore/history/bytes/provenance/migration | Existing source tools retained; no live corpus opened, no populated fidelity rehearsal run |
| Docker CPU / dependency hashes | Non-root installed wheel, normal bridge loopback publication, clean volumes, HTTP/MCP/host CLI, auth negatives, owner contention and restart persistence exercised; exact pinned hashed CPU/build inputs and digest-pinned base. Linux amd64 only; no live deployment or publication |
| SBOM/licenses and platform release | Not delivered; redistribution/provenance review and other platforms remain |

## Acceptance

`verification/acceptance.py` starts the installed package using `-I`, outside the source tree and with empty HOME. With `--docker-image`, the same scenarios start the installed image entrypoint on a normal isolated bridge, publish a random host-loopback port and use clean owned state/config volumes. The service runs as UID 10001 with read-only rootfs, dropped capabilities and no-new-privileges. Only a network-disabled one-shot helper initializes private config-volume files as namespace-root. The runner verifies installed image module hashes and removes its exact resources. See `container/README.md`. It checks complete source/installed/wheel Python-module sets and hashes when `--wheel` is supplied. Real HTTP, SDK MCP initialize/list/call and CLI exercise the existing corpus path. Synthetic state/evidence/credentials stay in the explicit new `--out`, outside source. In non-Docker mode a child audit guard records/refuses non-loopback connections; this proves the exercised no-backend path, not every optional operation. Docker mode configures no external backend but does not install that audit hook or claim a runtime egress firewall.

The exact command/results and dependency snapshot are in the private implementation receipt. The runner is the compact repeatable acceptance for this slice; it is not a full-product suite. No live deployment, corpus mutation, model download, GPU job or remote Git write is part of acceptance.

## Native workspace slice — implementation contract

Outcome: `projects.get` → `projects.savePrompt` → native readback through the same HTTP/MCP/CLI dispatch. InvokeAI remains authoritative. References explicitly bind connection ID, operator-declared backend generation and project ID; the existing zero/one pinned connection limit remains.

Supported codec: InvokeAI 7 split document v4, canvas compatibility at most v2, existing workspace with settingsLayering=1 and an explicitly selected `generate` widget instance. Edit only `widgetInstances[id].state.values.positivePrompt`; preserve every other JSON value, including opaque fields. No whole-document migration, workspace creation, prompt history rewrite, graph compilation or inherited-default resolution. Read returns opaque native halves and independent content/workspace revisions. Unsupported codec fails closed.

Save requires the caller's workspace revision. Durable SQLite request binding (including full scope, exact revision and prompt) precedes native PUT. Native workspace CAS, never content PUT or refreshed-revision retry. Persist exact prepared request and ACK before verification. Replay returns the same receipt without PUT; payload/scope changes conflict. A stale CAS is a durable failed receipt. Interrupted/failed PUT without durable ACK stays `ack_unknown`, including when native prompt equals the intended prompt. A durable ACK can be verified after restart using exact acknowledged workspace revision and full workspace data; intervening edits remain unknown. Recovery never writes. Local prepared documents are intent evidence, not an authoritative native store.

Acceptance runner must exercise HTTP and SDK MCP success/readback, opaque preservation, independent revisions, stale/concurrent CAS, request replay/change, restart and lost ACK, against an isolated target with no live proxy. Native upstream layering acceptance and fixture acceptance are reported separately. Existing core acceptance remains required; unchanged Docker image is not rebuilt for this source slice. Full-product completion still requires the inventory above.

Verification boundaries: `verification/native_workspace.py` exercises a deterministic HTTP contract fixture, including interrupted submissions and read-only reconciliation. A separate private integration harness exercised the pinned `be318726dc800ff3ba7c6f086fda603fd82abb86` projects router, original service, serializers, queries, transactions, DTOs and SQLAlchemy metadata on synthetic SQLite. Only config-type, synthetic caller and invoker/service dependency wiring were substituted; no serialization/transaction/CAS patching. Metadata-created tables and explicitly seeded lock rows are not migration acceptance. No full InvokeAI startup/auth, UI hydration, live state or GPU work was exercised.

Known conservative limits: readback exceptions and intervening workspace edits retain `ack_unknown` even with a durable ACK; the saved ACK remains evidence but cannot prove the current workspace. Ordinary HTTP/network/JSON preflight failures become durable failed receipts before PUT. Unexpected non-Fault preflight exceptions can leave a reserved queued intent; restart/replay never dispatches it. No remote authorship is inferred from matching prompts or documents.

## Native queue slice — implementation contract

Outcome: admit one no-model graph through authenticated HTTP/MCP, observe native pending/canceled state, and preserve intent/identity through replay, restart and lost ACK. Use the existing generation ledger, delivery worker and observer; InvokeAI alone executes its queue. No second executor, ledger or retry framework.

`generation.submitGraph` adds explicit connection_id/backend_generation/request_id/graph/runs (1–4). Requires a pinned connection, enable_writes and explicit `native_graph` in policy accepted_modes. It is an operator-supplied native graph, not a Canvas compiler or model recipe validator. Unsupported native node schemas can be refused by InvokeAI; do not treat admission as graph execution. Limit 128 nodes/512 edges; native batch data/workflow/project routing are not exposed by this operation. Therefore requested count equals runs, never runs squared. Existing generation.submit/batch.submit/recipes and workspace handlers remain available; model family execution is still unaccepted.

Intent binds normalized public input and pinned scope. Generated graph UUID, native retry key, resolved recipe seed/graph, runs and explicit batch defaults are committed before the only enqueue POST. Concurrent dispatch is fenced by the existing SQLite state transition. A queued, not-yet-fenced intent may be delivered once after restart; submitting/ack_unknown never POST again. Identical public replay returns the saved job without replanning. Changed payload under the same principal/request ID conflicts. Different principals retain existing shared-local attribution namespaces, not new authorization partitions.

Native receipt extension is pinned to InvokeAI be318726dc800ff3ba7c6f086fda603fd82abb86: GET enqueue_batch/receipt recovers exact IDs and accepted batch identity under the native account. Original service commits admission and receipt together. Validate requested/enqueued/unique IDs, persist receipt and IDs atomically before returning. A missing/malformed/unavailable receipt, including 404, remains unknown and never licenses another POST. Zero-admission ACK is failed, partial admission is represented by requested/enqueued counts; no automatic top-up. Native receipts are deliberately not acknowledged or purged in this slice; this preserves recovery evidence but consumes upstream unacknowledged receipt capacity. Safe post-durability acknowledgement/retention maintenance remains work.

Observation of keyed intents uses receipt IDs and batch identity, never origin equality. Unknown own-origin rows wait for receipt recovery. Missing queue history retains IDs/receipt and marks history_missing, not failure/cancellation. Cancellation requires exact known IDs and native item/batch readback; unknown intents cannot guess targets. Existing single-root pinned binding prevents backend/owner replacement; multi-connection migrations remain unsupported. Older unkeyed records retain their historical reconciliation compatibility, without a claim of new receipt recovery.

`verification/native_queue.py` exercises real Atlas HTTP/MCP through a loopback fault proxy against an explicitly supplied isolated native URL. Source and installed-wheel checks use a private harness with the original pinned queue router, SessionQueue, batch expansion/counting, database queries/transactions and metadata-created synthetic SQLite. Substitutions: graph DTO/serialization (integer-only probe, no native graph validation/execution), configuration, synthetic caller, invoker, events, media/maintenance dependencies and a Python 3.11 itertools.batched shim. Not full server bootstrap/auth/schema migration, processor execution, GPU/model acceptance or live deployment. Fault proxy drops ACKs/kills only the disposable Atlas child and can hide receipts/history. Core and workspace acceptance are regression gates; the dev1 Docker snapshot remains unchanged.
