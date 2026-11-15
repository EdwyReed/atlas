# Portable final refresh

A portable capture remains quarantined while OLD resumes. Before switching writers,
refresh its owned payload and **entire current ledger** from the explicitly selected
stopped OLD root. Do not activate the original capture and discard later writes.

This release adds `atlas_engine.portable_refresh` and reuses the offline delta
staging/recovery path. It does not stop services, deploy a container, reconcile
unknown effects, run native actions or install models.

## Contract

- Use the original, never-activated portable quarantine. Reconstruct its original
  pack manifest with the actual capture executable's `state_backup.versions()`;
  require its exact original manifest hash. Historical adoption provenance is not
  the current source binding. Use `portable-state.json.source_root` explicitly.
- Hold both existing service/corpus owner locks on source and destination. Mount
  the source read-only. Clean SQLite reads use immutable mode; committed registry
  WAL is read through SQLite and backed up into a closed DELETE-mode database.
  A nonempty Qdrant WAL/rollback journal refuses the cut. Never load source Qdrant.
- Stage only changed owned files plus the complete registry backup. Immutable
  references, vocabulary and portable media remain pinned and unchanged. Verify
  original historical reference files through explicit individual read-only mounts.
- Preserve every explicit configured-source exclusion exactly, INCLUDING terminal
  rows and kinds outside the nonterminal native selection. Union hash-bound archived
  admission lineage with current nonterminal effect rows; compare exact sorted IDs,
  kinds, states and full-row SHA256, not just count. Preserve uncertain rows; changed holds
  require a separate operator review, not an automatic terminal inference. Retain
  completed states and all current source tables, events and result links.
- Reject destination writes, changed source cuts, corrupt staged bytes and stale
  activation reports. Persist incomplete quarantine before replacement; interrupted
  targets refuse startup/activation and can resume only the same pinned cut.
- Seal a destination/runtime/ledger/payload-bound final manifest. Activation binds
  that final manifest and refresh receipt, never only the old capture hash.

## Operator commands

The deployment owner supplies actual private paths and the exact immutable release
image. These commands have side effects only when explicitly run by that owner.
Keep the independent OLD restart fallback outside the refresh process. Never run
OLD and successor writers together. Do not restart OLD between preparation and
activation; if the stopped source changes, retain evidence and do not activate.

Render the binding first; this reads only portable metadata/restore marker from
an explicitly inspected read-only named volume, and captures versions from the
original pack Python. It writes a NEW owner-only evidence directory. It does not
open databases or alter application data.

```sh
python3 -I -B verification/portable_binding.py \
  --image "$PINNED_IMAGE_ID" --volume "$QUARANTINE_VOLUME" \
  --source-root "$CURRENT_OLD_ROOT" --capture-manifest "$ORIGINAL_MANIFEST_SHA256" \
  --capture-python "$ORIGINAL_PACK_PYTHON" --expected-held "$REVIEWED_HELD_COUNT" --out "$NEW_CUT_DIR"
```

After the owner has stopped/fenced only Atlas and established its rollback path:

```sh
python3 -I -B verification/portable_cutover.py prepare --binding "$NEW_CUT_DIR/binding.json"
# Review refresh-plan.json and its emitted plan SHA256; never guess the hash.
python3 -I -B verification/portable_cutover.py apply --binding "$NEW_CUT_DIR/binding.json" --sha256 "$REVIEWED_PLAN_SHA256"
python3 -I -B verification/portable_cutover.py verify --binding "$NEW_CUT_DIR/binding.json"
python3 -I -B verification/portable_cutover.py report --binding "$NEW_CUT_DIR/binding.json" \
  --operator "$OPERATOR" --assessment "$EXPLICIT_HISTORICAL_EFFECT_ASSESSMENT"
# Review activation.json. This checksum is distinct from the plan checksum.
python3 -I -B verification/portable_cutover.py activate --binding "$NEW_CUT_DIR/binding.json" --sha256 "$REVIEWED_ACTIVATION_REPORT_SHA256"
```

The runner uses network-none containers, a read-only root filesystem, explicit
source/historical-file mounts and namespace-root DAC_OVERRIDE/CHOWN only. It
returns target-owned root-created files to UID10001 even on failure. The ordinary
application stays UID10001. It never mounts HOME, a credential tree or source
configuration. It never creates an absent destination volume.

Start the successor only through the owner's reviewed Compose/service plan,
preserving existing policy, references, optional media and SSH provisioning. The
old SSH release's source-pinned renderer cannot be used unchanged with new source;
the private successor handoff includes a separately pinned render-only copy.
Startup/restart and read-only observer acceptance on real data remain owner work.

## Rebase a completed, never-activated final cut

After a failed verification and OLD resumption, the previous cut must not activate.
Keep the existing quarantine and prior evidence. This is not same-cut interruption
recovery: an incomplete target can ONLY resume its exact plan and unchanged source.
Never change a marker, lower the expected count, or reconstruct a baseline from a
target that has been activated or has successor writes.

Render a NEW binding/evidence directory with the ORIGINAL capture hash and the
explicit reviewed previous `restore-state.refresh.sha256`:

```sh
python3 -I -B verification/portable_binding.py \
  --image "$PINNED_IMAGE_ID" --volume "$QUARANTINE_VOLUME" \
  --source-root "$CURRENT_OLD_ROOT" --capture-manifest "$ORIGINAL_MANIFEST_SHA256" \
  --capture-python "$ORIGINAL_PACK_PYTHON" --expected-held "$REVIEWED_HELD_COUNT" \
  --rebase-refresh-sha256 "$REVIEWED_PREVIOUS_REFRESH_SHA256" --out "$NEW_CUT_DIR"
```

The renderer reads the archived configured-source admission record and pins every
protected row in `binding.json.expected_held_rows`. `prepare` must run only after
the deployment owner fences OLD, using the SAME explicit phases above. It proves
the previous destination-bound final manifest, ledger and payload still match;
checks the historical executable against the reviewed prior cut (not the new
successor executable); verifies archived exclusion records by their original
hashes without following absolute pointers into OLD; and stages a new current cut.
The new final manifest retains the original capture hash and the bound prior final
manifest. All normal apply/activation runtime checks still require the new runtime.
No second full reference/model archive is needed. Changed holds require a separate
review; newly discovered holds cannot silently pass an operator binding with a reviewed count.

`verify` emits all exact protected rows. Both report creation and activation check
them against the binding, the current ledger and the current plan hash. Old reports,
old plans, source drift, destination writes, and already activated targets refuse.

## Frozen source-only successor

Wheel version remains `0.4.0.dev19` for this private package-only release. Select by
exact SHA256, not version or a mutable tag:

- Wheel SHA256: `467b2eff78d6cff8b2043dd50b513933256ad82cb0d94a4f724fdedb94e5cd30`.
- Core: `sha256:b1e78ba3768cd88182c046dcafdb522b328c641da3a4e2580e7882c5817d2640`.
- Private media: `sha256:e37846fee0230b05fdac5ce71345c3277a9fb711bcd73fe0c504bce7214340b2`.

`container/Dockerfile.delta` installs only the selected wheel over the accepted
delta2 bases. Every retained dependency layer and SSH/media helper stays byte-identical.
Do not use delta1/delta2 for another cutover: delta1 lacks the clean read-only
registry snapshot fix; delta2 drops terminal configured-source holds and cannot
rebase. Preserve those immutable images and original final14/SSH rollback material.

## Executed engineering acceptance

`verification/portable_refresh.py` with the isolated installed wheel and the
original dev18 capture Python exercises old-pack compatibility, committed WAL,
full authoritative ledger and raw corpus equality, seven unchanged holds,
wrong-runtime/Qdrant-WAL/hold-change refusals, ownership locks, foreign destination
writes, reference/stage corruption, interruption/startup refusal and same-cut
resume, stale report refusal, HTTP/MCP receipt reads and restart.

`verification/portable_docker_refresh.py` exercises the actual binding renderer
and operator runner through prepare/apply/verify/report/checksum-bound activate
on disposable volumes: core with a clean read-only source and media with a
committed WAL. Exact source file sets and bytes remain unchanged, including
sidecars. The activated target is readable by application UID10001 with no
wrong-owner files. Owned fixture volumes are removed.

`verification/portable_rebase.py` runs the actual shipped delta2 executable on a
redacted real-shaped fixture: two observing generations, one succeeded generation
still explicitly protected, and four ack_unknown native-admin jobs. It reproduces
six selected holds while all seven rows remain in SQLite. The installed successor
rebases this old-runtime cut after new OLD records, preserves the exact seven,
recovers a second-cut interruption, and refuses stale plans/reports and activated
successor writes. Separate explicit-exclusion checks span effect/CPU/batch kinds.
Installed HTTP/MCP receipt reads and restart occur after a repeated final refresh;
both Docker images exercise repeat final cut, exact-hash mismatch at unchanged
count, stale-source activation and prior-report refusal on disposable volumes.

`verification/portable_state.py` retains original populated pack/restore,
relocation, quarantine, explicit activation, new local corpus work, HTTP/MCP and
historical-effect restart regressions. `verification/delta_release_parity.py`
checks exact 73-file package names and SHA256 across source, staging, build/lib,
reproducible wheel, isolated installation and both immutable images, plus embedded
wheel hashes, unchanged base-layer ancestry and SSH/media helper bytes.

These are synthetic engineering checks. They neither reopen the real capture nor
claim production activation, GPU/native acceptance or full-product completion.
