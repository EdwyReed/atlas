# Atlas

Standalone headless Atlas, development package 0.4.0.dev4. The canonical corpus is embedded Qdrant; operational state is SQLite. HTTP, Streamable HTTP MCP and a thin CLI share the existing Engine dispatch. Hermes and InvokeAI are optional clients/backend integrations, not startup dependencies.

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

## Native workspace prompt subset

The registry currently has 113 operations (counted from the shared input schemas). Listing an operation does not certify its backend; `OPERATIONS.json` records the exercised boundary.

`projects.get` and `projects.savePrompt` implement a narrow InvokeAI 7 split-v4 codec: an existing layered workspace (`settingsLayering=1`), canvas compatibility at most v2 and an explicitly selected `generate` widget. A save changes only `widgetInstances[id].state.values.positivePrompt`; opaque content and all other workspace values are preserved. This does not create workspaces, migrate documents, resolve inherited defaults, compile workflows or support arbitrary Canvas editing.

Both operations require `connection_id`, `backend_generation` and `project_id`. Save additionally requires `request_id`, `expected_workspace_revision`, `widget_instance_id` and `prompt`, and the connection's `enable_writes` must be true. Obtain the workspace revision from `projects.get`. A successful operation envelope can contain a failed or `ack_unknown` job receipt: inspect its `state`. Use a NEW request ID only for an explicit new intent, never to bypass an uncertain ACK. Replays and restart recovery never repeat a native PUT. An acknowledged save followed by another workspace edit can remain `ack_unknown`; matching prompt text does not prove authorship.

The committed runner below is a deterministic HTTP contract fixture. A separate isolated integration also exercised the pinned native projects router, original service/DTOs/serialization/transactions/CAS and SQLite schema with a synthetic caller and dependency wiring. It is not full-server/auth/migration or frontend hydration acceptance. No live backend was written. The broader uncertainty/crash scenarios remain fixture-tested.

## Repeat the slice acceptance

From the source directory, using the environment where the package is installed:

```sh
/absolute/venv/bin/python -I -B verification/acceptance.py \
  --wheel /absolute/path/to/atlas_engine-0.4.0.dev4-py3-none-any.whl \
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

This runner submits integer graphs, cancels its exact items and injects lost replies/process crashes. Do not point it at a live service. The private native harness exercised original router/service/SQLite admission and cancellation with a fixture graph codec, not native graph validation/execution, full bootstrap/auth/migrations or GPU jobs. See SPEC.md for the exact boundary.

## CPU Docker delivery

[Build and isolated bridge run](container/README.md) documents the non-root CPU image, private mounted configuration, writable state volume and repeatable `--docker-image` boundary acceptance. Its base digest and hashed dependency inputs are pinned independently of the package version ranges. HTTP, real MCP and the installed host CLI were exercised against the built image on clean volumes, including auth negatives, duplicate ownership refusal and restart persistence.

The previously accepted Docker snapshot is 0.4.0.dev1; it has not been rebuilt or accepted with this workspace slice. No populated migration/restore, full native server execution, GPU job or packaged media inference is claimed here. Source licensing/third-party redistribution, SBOM and release provenance still need the release review before publication. No live deployment or public service is part of this delivery.
