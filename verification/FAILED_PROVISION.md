# Failed portable media provisioning

This recovery is restricted to a completed activated cut whose successor never
entered runtime admission, with a complete partial media copy. It is not a marker
reset or permission to replace a started successor.

`atlas_engine.portable_recovery` requires a checksum-reviewed JSON report:
`format=atlas-failed-provision-recovery-v1`, literal `target_root`, complete
`state`, original capture manifest digest, exact `refresh` and `admission`
references, `activation_report_sha256` equal to admission SHA256, current `runtime`,
`failure_phase=media-provision-before-runtime-admission`, true `runtime_fenced`
and `never_started`, nonempty `operator` and `assessment`, `evidence` as a LIST
of `adoption.pin(path)` objects, and complete `target_inventory` from
`adoption.source_inventory(root)`. Evidence files must be mounted at their pinned
absolute paths. Independent controller/process fencing evidence is required;
absence of a start receipt alone is insufficient.

Both owner locks, original final manifest/payload, full logical ledger, exact
protected rows, portable references and target inventory are checked. Runtime
admission witness or successor changes refuse recovery. Previous marker and
admission survive in an immutable recovery record. Recovery returns to quarantine;
a fresh stopped-OLD final refresh and new checksum-bound activation are required.
Do not use the previous controller, activation or refresh plan after OLD resumes.

The media helper keeps CHOWN + DAC_OVERRIDE only, read-only rootfs, network none
and no-new-privileges. It sets modes before handing files to UID10001; FOWNER is
not needed. For an existing *complete* partial copy, prepare a separate review
plan with `init.py --restored --state-root ROOT --prepare-recovery PLAN`, then
finalize with `--recovery-plan PLAN --sha256 REVIEWED_HASH`. This never recopies
weights or reinitializes config. Changed bytes, foreign owners and unreviewed
modes refuse. Retain the plan and original partial kit on failure.

Disposable verification:

- `verification/failed_provision_recovery.py`: original executable activation,
  seven exact held rows including succeeded history, payload/ledger/admission
  refusal, fresh OLD progress, interrupted same-cut refresh, stale activation.
- `verification/media_docker.py --production-envelope --old-helper OLD_HELPER`:
  real original EPERM under production helper caps/mount targets; reviewed repair;
  populated portable activation; actual Compose UID10001/read-only/no-new-privileges
  service with production CPU/memory/PID/tmpfs limits; authenticated HTTP, MCP,
  installed client, CPU mask/censor artifacts and restart; exact seven rows and
  durable runtime witness; unchanged config; owned-volume cleanup.

SSH key loading remains separately transport-accepted and unchanged. This CPU
fixture does not mount an operator key, contact a native backend or establish
production activation acceptance. The deployment owner supplies the reviewed release lock and
deployment binding. Source docs contain no keys, account bindings or production
volume names.
