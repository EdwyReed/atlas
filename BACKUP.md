# Offline backup / restore (dev5 contract)

Outcome: stopped populated Atlas root → private backup directory → NEW root,
without converting corpus points, revisions, tombstones, evidence or byte files.
This is same-format Linux embedded-Qdrant recovery, not historical corpus migration.

dev11's separate [offline legacy attachment slice](ATTACHMENT.md) preserves the complete captured ledger and unchanged external mirror dependencies in a new quarantined root. It does not make the original online capture atomic. `atlas-state backup` refuses these partial roots rather than silently excluding their external attachments; `atlas-state reconcile` cannot activate them. The same-format backup contract below is unchanged for ordinary canonical roots.

`atlas-state backup ROOT BACKUP` holds the existing root/service.lock and
corpus/owner.lock for the entire snapshot. An active owner is refused. Registry
uses SQLite backup (includes committed WAL); the closed Qdrant tree is copied
verbatim. SQLite integrity and ordered raw point hashes are checked without
opening source Qdrant or calling head(). Manifest pins package/Python/Qdrant
versions, format, files, byte hashes, point kinds and checkpoint consistency.

Included: registry.sqlite3, corpus, artifacts, media, connection-binding.json and
previous restore provenance. No config/credentials, workers, weights, caches,
external registered originals, vocabulary, reference mirrors or projection stores.
Root entries outside this explicit set are listed as excluded, never followed.
Backups contain private journal/corpus bytes and may contain historical sensitive
payloads; they are NOT sanitized public artifacts. Config secrets are not read.

`atlas-state restore BACKUP NEW_ROOT` refuses existing targets, overlap, symlinks,
nonregular entries, traversal, case-fold collisions, integrity/version mismatch.
Incomplete backups have no manifest and cannot restore; use a new destination.
Source/backup remain untouched. There is no destructive overwrite or merge.
Restore durably binds a new unreconciled marker before copying any journal bytes;
the payload's previous marker is retained as provenance, never copied over it.
An interrupted copy/verification remains marked incomplete and rejects Engine
construction and reconciliation. Retry into a new root, not the partial one.
Only after exact preimage verification and payload fsync is the marker atomically
completed (still quarantined). Exact file hashes exclude this managed marker;
SQLite logical state and all other payload bytes are preserved before Engine open.

Storage relocation is explicit in restore-state.json: old-root-relative artifact
and media prior references resolve under the new root at use time; persisted
journal, source paths, manifests and corpus bytes stay unchanged. External paths
are not remapped or copied. Artifact reads outside owned artifacts/media are
refused. Native connection binding remains exact and must be configured separately.

Every restored root starts quarantined: no background workers/events/recovery;
only an explicit local-read operation allowlist is admitted. Thus stale queued
intents, receipt ACK maintenance, projection and media cannot mutate external
systems, even with write-enabled config. Corpus logical reads may repair heads
locally; preimage verification must precede them. Existing-request replay returns
the durable receipt without executing the effect.

`atlas-state reconcile ROOT REPORT.json` is an offline operator action holding
both locks. Report must bind the current committed logical registry hash (including
WAL), backup manifest hash and exact target root, contain nonempty explicit
assessments for generation/workspace/media/acquisition/projection/uploads and
confirm external effects reconciled. Nonterminal external jobs and ambiguous
uploads must first be resolved by a separately reviewed reconciliation procedure;
this command will not guess outcomes or cancel/replay them. Accepted report is
retained in the restore marker; only the next Engine start admits work. It is an
operator attestation, not automatic proof against a remote backend.

Compact acceptance: synthetic five collections with revisions/deletion/evidence,
missing head, unknown fields, Unicode, negative occurrences; registry uncertain
job/events/upload BLOB; artifact/prior bytes; owner refusal; WAL backup; exact raw
preimage before repair; restore elsewhere; local public reads/idempotent replay,
no network or worker replay; overwrite/traversal/symlink/corruption refusal.
Legacy/private corpus migration and native processor execution remain unaccepted.

## Commands

Run with the pinned installed package, after the owner has stopped:

```sh
atlas-state backup /absolute/stopped-root /absolute/NEW-backup
atlas-state restore /absolute/NEW-backup /absolute/NEW-root
# Configure the SAME connection binding and new private client credentials
# separately. Starting Atlas now permits local reads/known-intent receipt replay.
atlas-state reconcile /absolute/NEW-root /absolute/operator-report.json
# Only after successful reconciliation, restart Atlas to activate admission.
```

The private operator report has `target_root`, `backup_manifest_sha256` from
restore-state.json, `registry_logical_sha256`, `external_effects_reconciled: true`,
and nonempty string assessments named `generation`, `workspace`, `media`,
`acquisition`, `projection`, `uploads`. Compute the WAL-aware ledger binding with
the installed `atlas_engine.state_backup.registry_digest(Path(root))` while the
daemon is stopped. Review native queue/receipts/workspace revisions, uploads,
worker results and projection delivery externally before attesting; matching
content alone does not resolve an unknown effect. This tool has no command that
silently marks unknown jobs terminal. Retain the report privately.
