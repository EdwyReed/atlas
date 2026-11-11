# Atlas

Standalone headless Atlas, first runnable slice. The canonical corpus is embedded Qdrant; operational state is SQLite. HTTP, Streamable HTTP MCP and a thin CLI share the existing Engine dispatch. Hermes and InvokeAI are optional clients/backend integrations, not startup dependencies.

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

The CLI's `--endpoint` defaults to `http://127.0.0.1:12088`. HTTP operations are `POST /v1/ops/{operation}` with a JSON object and bearer authentication. Input schemas are `GET /v1/schema`. MCP is Streamable HTTP at `/mcp/`, with the same authentication header; tool names replace operation dots with underscores. Host and browser Origin must match the configured loopback listener (or localhost on its port). Non-browser clients omit Origin. No anonymous endpoint is exposed.

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

## Repeat the slice acceptance

From the source directory, using the environment where the package is installed:

```sh
/absolute/venv/bin/python -I -B verification/acceptance.py \
  --wheel /absolute/path/to/atlas_engine-0.4.0.dev1-py3-none-any.whl \
  --out /absolute/new/private-acceptance-directory
```

The output directory must not exist. It holds synthetic data, logs, generated credentials and `result.json`; keep it OUTSIDE the source/release. The Linux runner exercises a nondefault `127.0.0.2` bind, rejects unintended non-loopback child connections and uses no live corpus/backend. It compares installed/source/wheel module sets and bytes, exercises real MCP initialization/list/call, CLI/HTTP replay, auth failures, owner contention and restart. Omitting `--wheel` skips the wheel comparison only.

No Docker build, populated migration/restore, native execution, GPU job or packaged media inference is claimed here. Dependency version ranges are not a hashed release lock. Source licensing/third-party redistribution and release provenance still need the release review before publication.
