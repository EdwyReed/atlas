# Offline backup / restore (dev5 contract)

Outcome: stopped populated Atlas root → private backup directory → NEW root,
without converting corpus points, revisions, tombstones, evidence or byte files.
This is same-format Linux embedded-Qdrant recovery, not historical corpus migration.

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
Incomplete writes have no manifest and cannot restore; use a new destination.
Source/backup remain untouched. There is no destructive overwrite or merge.
Exact file hashes and SQLite logical state are verified before any Engine open.

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
both locks. Report must bind the current registry hash, contain nonempty explicit
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
