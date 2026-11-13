# Offline historical attachments (dev11)

`atlas-attachment` adopts an existing sealed `atlas-checkpoint-attachment-v1` into a **new quarantined standalone root**. This is a partial migration with externally retained source files. It does not create a self-contained portable copy or certify complete canonical migration.

## Concrete mapping

| Captured data | Destination / supported view |
|---|---|
| Entire operational SQLite ledger, every table, column, trigger, index and row | Exact file copy into `ROOT/registry.sqlite3`; full schema/table/byte comparison before startup. Existing Engine initialization may subsequently add its ordinary empty tables/indexes. Every original table must remain identical. |
| `knowledge_revisions`, `knowledge_heads`, import receipts | Retained in their existing domain format, including non-head revisions, original IDs/hashes/supersedes and opaque JSON. `knowledge.get`, `knowledge.search`, new paginated `knowledge.history`. No new body imports or inferred authors. |
| `legacy_rows` and other unknown/history/blob tables | Remain unchanged and inert in the same ledger. `legacy.rows.page` exposes original encoded `data` and stored SHA256, with exact source/table/row provenance. Binary SQLite columns in other tables remain binary columns; they are not converted to JSON. |
| Historical baseline mirror, delta, observation/hold tables, manifests | Remain unchanged at explicitly pinned external paths. The receipt records every byte pin and dependency. No source copy, hardlink, source index build, Qdrant open or source repair. |
| Canonical corpus | A new empty five-collection payload-only Qdrant authority. **Zero legacy points are canonicalized.** Legacy `entity_type`/target values remain historical hints, not canonical identities. |
| Referenced source documents/artifact bytes | Anchors and available inert blob rows are retained. Availability of referenced files outside the pinned inventory is **unassessed**, not “zero missing”. No fetch or invented local-path mapping. Full migration acceptance remains blocked on complete fidelity/byte availability and reviewed semantic mapping. |

The source capture limitation is preserved in both mapping and receipt:

```text
two exhaustive equal online passes; NOT atomic; writers NOT fenced
```

A byte seal binds the exact captured files. It proves neither an atomic upstream cut nor current/full live-source completeness. Equal passes cannot supply those guarantees.

## Import

Create an operator-reviewed private JSON mapping with exactly these top-level fields:

```json
{
  "format": "atlas-legacy-attachment-mapping-v1",
  "mapping": "ledger-preserve-v1",
  "attachment": "/absolute/sealed/checkpoint.json",
  "source_consistency": "two exhaustive equal online passes; NOT atomic; writers NOT fenced",
  "files": [
    {"path": "/absolute/sealed/checkpoint.json", "bytes": 1234, "sha256": "EXACT_64_LOWERCASE_HEX_DIGITS"}
  ]
}
```

The example inventory is incomplete and deliberately cannot import. Supply actual size/SHA256 pins for **all** dependencies: the attachment, its baseline, ledger, stream-delta receipt/overlay/observations, every declared attachment `*_sha256` path, and other retained manifests/evidence to preserve. Never fabricate checksums, infer owner mappings or qualify a live SQLite file. The command rejects symlinked/hardlinked sources, nonempty WAL/journal sidecars, observed file locks, byte-pin mismatches, unknown attachment formats, active jobs/observations or populated operational uploads/artifact paths needing a different reviewed mapping. Linux lock observation is a qualification check, not a writer fence.

```sh
atlas-attachment import --mapping /private/mapping.json --destination /private/NEW_ROOT
```

The destination parent must exist. The importer reserves a bounded ledger-copy budget plus 2 GiB free space, writes/fsyncs an incomplete quarantine marker before copying ledger bytes, compares the complete source and destination, creates the empty canonical corpus, rechecks source pins and completes the marker. Failed destinations remain incomplete and must not be started or repaired in place. Existing destinations are never overwritten.

The root's `legacy-attachment.json` is a private receipt: exact mapping and digest, source schema/table hashes, baseline collection counts, inert source/table inventory, external-copy limitation and unresolved scope. Keep it and the source files private. Source paths are not rewritten.

## Standalone reads

Start the ordinary daemon with a config containing only the new root, private clients file, loopback host/port and optionally a local vocabulary. Do **not** set connections, reference backends, projection or media: partial attachment roots reject them before constructing adapters. The receipt supplies the sealed reference reader automatically. Every source SQLite connection uses `mode=ro&immutable=1` after sidecar/lock qualification; complete byte pins are verified on each startup. Large mirror hashing increases startup time intentionally.

Public HTTP, SDK MCP and CLI use the same schemas:

- `attachment.status`: private receipt, dependency and mapping inventory.
- `attachment.get`: `view` (`baseline` or `overlay`), `collection`, exact `id_json` (for example `"1"` for numeric ID 1 or `"\"1\""` for string ID `1`). Returns the full captured point, vectors/unknown fields, SHA256 and source/view provenance. Baseline is historical; its raw read deliberately does not suppress points held by the later observation.
- `attachment.page`: same view/collection, `after` exact serialized ID cursor, `limit` 1–30. Stable captured-identity order; `has_more` and `next_after` are explicit.
- `knowledge.history`: literal `entity_id`, `after_revision`, `limit` 1–100. Returns original records/hashes, including obsolete revisions; no head movement.
- `legacy.rows.page`: exact source path/identifier and table name from the receipt's `inert_sources`, integer `after` row cursor and `limit` 1–100. Data is historical encoded text, not an executable request.
- Existing literal `search`/`evidence`: use retained typed reference views with newer raw identities and observation holds suppressing stale older candidates. Knowledge and canonical results stay separate. Optional vocabulary absence no longer prevents literal corpus/reference/knowledge search; creative operations still require it. Unknown vector spaces refuse semantic/hybrid queries before encoding. Typed views cover only their existing supported resource/tag records, not all raw points.

Workers, native recovery, ACK maintenance and projections stay suppressed. Mutations/new work refuse admission; historical legacy-key ambiguity may return the existing `LEGACY_UNBOUND` refusal. Repeated reads and restart retain the same history. `atlas-state reconcile` cannot activate this partial mapping, and `atlas-state backup` explicitly refuses to call its external dependencies a self-contained backup. Preserve the original attachment unchanged. Completion of full canonical migration requires a separate reviewed fidelity outcome; no generic conversion DSL is introduced.

## Repeatable verification

Use an installed wheel and an absolute isolated Python executable:

```sh
/path/to/env/bin/python -I -B verification/legacy_attachment.py \
  --mapping /private/mapping.json --out /private/NEW_EVIDENCE \
  --wheel /private/atlas_engine-0.4.0.dev11-py3-none-any.whl
```

The runner imports into a new private root, compares every source table/schema, reads every knowledge revision over authenticated HTTP, checks SDK MCP history/raw/inert views, literal search, restart, quarantine and source-byte preservation. `--root` verifies a root already created by the installed CLI; source tables must still match after ordinary schema initialization. Source-write/non-immutable SQLite/non-loopback network audit guards apply to exercised paths. They do not claim a global network sandbox or fence.

Omit `--mapping` for a separately labelled small synthetic counterexample: three revisions with opaque/tombstone/attribution fields, binary unknown history, inert legacy rows, distinct numeric/string point IDs, vector quarantine, overlay suppression and holds, interrupted import and changed-pin refusal. Synthetic success is not real retained-source acceptance. Run `verification/acceptance.py` and `verification/backup_restore.py --cli` separately for core and same-format backup regressions. All evidence, credentials and retained data stay outside the source/release payload.
