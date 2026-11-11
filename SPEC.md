# Atlas — headless slice 1

## Contract

Install the Python package into a new environment, run `atlas --config atlas.json`, and use a private loopback daemon without Hermes, InvokeAI, a vocabulary, model weights or existing data. One Engine owns the existing dispatch, SQLite ledger, canonical embedded Qdrant and artifact files. CLI is an HTTP client; MCP uses the existing SDK and the same dispatch.

This slice must demonstrate authenticated HTTP status/capabilities/input schemas, a literal corpus mutation with readback, CLI/MCP replay of that exact intent, matching validation, restart preservation and refusal of another storage owner. Missing/wrong credentials and hostile Host/Origin must fail before dispatch. Missing optional backends report unavailable, not synthetic success or a private fallback.

## Implementation boundary

- Package `atlas-engine` 0.4.0.dev1; executable `atlas` (alias `atlas-engine`) and `atlas-client`.
- Configuration is explicit JSON, paths relative to its directory. Defaults → config → explicit `--root/--host/--port`. No environment configuration, `.env` discovery or Hermes imports. Secret references are private files. No embedded plaintext InvokeAI token field.
- Loopback IP bind by default; explicit container mode binds 0.0.0.0 only with an exact `container_endpoint` of `http://127.0.0.1:HOST_PORT`. Container Host/Origin allowlists contain only that endpoint; Docker publication MUST bind host 127.0.0.1. One process; no Uvicorn workers or reload. Every HTTP path, including MCP and unknown artifact paths, passes the same bearer/Host/Origin gate. Old public UI is not mounted or shipped.
- Corpus is `root/corpus`; ledger/artifacts/cache belong to `root`. Existing lifetime locks and corpus revision CAS are reused. This is not distributed Qdrant CAS.
- `connections` supports zero or one InvokeAI connection. More are rejected rather than silently choosing one. ID, URL, operator-declared backend generation/account scope are pinned to the state root; changing even an empty binding requires a new root or a future reviewed migration. Token rotation does not prove backend identity. Multi-connection scoped intents/refs are NOT implemented.
- Existing optional vocabulary, reference mirrors/typed references/deltas, local projection, policy and media bindings remain configurable. These paths were retained, not accepted by the empty-core smoke. Sealed attachment preparation, HTTP projection and migration utilities remain source mechanisms; the new ordinary launcher does not expose every old migration flag.
- `fastembed` is the `semantic` extra, loaded lazily with local-only weights. Missing vocabulary fails explicitly. Missing InvokeAI/reference adapters cannot open a network socket. Public acquisition endpoints remain available for explicit operations; no global production egress ban is claimed.
- Input schemas are inherited. New output/error schemas, DTO sanitization across all legacy operations, native CAS/receipts and the complete cycle-03 envelope are not delivered here.

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
| Generation/batch/queue/observation/cancel | Retained; no InvokeAI configured or native submission run; new native receipt/scoped routing contract pending |
| Boards/images/uploads/artifact fetch/publish | Retained; no native write or artifact transfer run |
| Inpaint/upscale/refinement and concrete graphs | Existing recipe mechanisms retained; GPU/mask polarity/lineage acceptance pending; refinement coverage not established |
| Mask/censor/media workers | Retained and configurable, off without bindings; packaged CPU inference/early async admission/polling acceptance pending |
| Native projects/workspace CAS/codecs and callable saved workflows | Target requirements retained; not implemented by this slice; no arbitrary workflow parity claimed |
| Full Canvas/compiler and native image-map semantic search | Not delivered; concrete versioned subsets/negotiated adapters remain target work |
| Backup/restore/history/bytes/provenance/migration | Existing source tools retained; no live corpus opened, no populated fidelity rehearsal run |
| Docker CPU / dependency hashes | Non-root installed wheel, normal bridge loopback publication, clean volumes, HTTP/MCP/host CLI, auth negatives, owner contention and restart persistence exercised; exact pinned hashed CPU/build inputs and digest-pinned base. Linux amd64 only; no live deployment or publication |
| SBOM/licenses and platform release | Not delivered; redistribution/provenance review and other platforms remain |

## Acceptance

`verification/acceptance.py` starts the installed package using `-I`, outside the source tree and with empty HOME. With `--docker-image`, the same scenarios start the installed image entrypoint on a normal isolated bridge, publish a random host-loopback port and use clean owned state/config volumes. The service runs as UID 10001 with read-only rootfs, dropped capabilities and no-new-privileges. Only a network-disabled one-shot helper initializes private config-volume files as namespace-root. The runner verifies installed image module hashes and removes its exact resources. See `container/README.md`. It checks complete source/installed/wheel Python-module sets and hashes when `--wheel` is supplied. Real HTTP, SDK MCP initialize/list/call and CLI exercise the existing corpus path. Synthetic state/evidence/credentials stay in the explicit new `--out`, outside source. In non-Docker mode a child audit guard records/refuses non-loopback connections; this proves the exercised no-backend path, not every optional operation. Docker mode configures no external backend but does not install that audit hook or claim a runtime egress firewall.

The exact command/results and dependency snapshot are in the private implementation receipt. The runner is the compact repeatable acceptance for this slice; it is not a full-product suite. No live deployment, corpus mutation, model download, GPU job or remote Git write is part of acceptance.

## Next coherent slice

The non-root CPU container boundary is delivered and exercised. Next, choose one disposable InvokeAI 7 workspace codec and implement scoped native intent binding before enabling multi-connection writes. Full-product completion still requires the remaining families above.
