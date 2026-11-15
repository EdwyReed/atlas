"""Evidence-bound retirement of a failed, activated portable provisioning cut.

This is NOT permission to overwrite a started successor. Both owner locks, the
entire original final cut and the activation ledger must still match. Operator
fencing evidence is mandatory in addition to (not instead of) those checks.
"""
import argparse
from pathlib import Path
from . import adoption as ad, adoption_control as ctl, portable as pt, portable_refresh as pr, state_backup as sb

FORMAT = 'atlas-failed-provision-recovery-v1'
WITNESS = '.portable-runtime-admitted.json'


def validate(root, report):
    state = sb.restore_state(root)
    if (not state or not state.get('portable_restore') or not state.get('preimage_verified')
            or not state.get('reconciled') or not state.get('held_admission')
            or state.get('refresh_pending') or (root/WITNESS).exists()
            or not (root/'media-kit'/'incomplete').is_file()):
        raise ValueError('activated completed portable cut without runtime admission required')
    admission = ctl.bound_record(state['held_admission'])
    cut = ctl.bound_record(state['refresh'])
    final = ctl.bound_record(state['portable_final_manifest'])
    # Validate under the ORIGINAL executable identity, not the recovery package.
    pr.validate_final(root, state, expected_runtime=cut['runtime'])
    if (admission.get('admission') != pt.ADMISSION
            or admission.get('runtime') != cut['runtime']
            or admission.get('target_root') != str(root)
            or admission.get('refresh') != state['refresh']
            or admission.get('backup_manifest_sha256') != state['backup_manifest_sha256']
            or admission.get('registry_logical_sha256') != sb.registry_digest(root)
            or admission.get('portable_metadata_sha256') != sb.sha(root/'portable-state.json')
            or admission.get('held_rows') != pt.protected_rows(root, state)):
        raise ValueError('activation/whole ledger/held rows differ; preserve successor')
    if (report.get('format') != FORMAT or report.get('target_root') != str(root)
            or report.get('state') != state
            or report.get('original_capture_manifest_sha256') != final['capture_manifest_sha256']
            or report.get('refresh') != state['refresh']
            or report.get('admission') != state['held_admission']
            or report.get('activation_report_sha256') != state['held_admission']['sha256']
            or report.get('runtime') != sb.versions()
            or report.get('failure_phase') != 'media-provision-before-runtime-admission'
            or report.get('runtime_fenced') is not True or report.get('never_started') is not True
            or not report.get('operator') or not report.get('assessment')
            or not report.get('evidence')):
        raise ValueError('explicit failed-provision/fenced-runtime/capture/refresh/admission assessment required')
    ad.check_pins(report['evidence'])
    # Pin ALL files, including runtime data outside the backup payload and the
    # partial kit. Absence of a controller start receipt alone is never proof.
    if report.get('target_inventory') != ad.source_inventory(root):
        raise ValueError('target runtime/partial provisioning inventory changed')
    pt.References(root).validate()
    return state


def recover(root, report_path, sha256):
    root, report_path = sb.safe_path(root), sb.safe_path(report_path)
    if sb.sha(report_path) != sha256:
        raise ValueError('recovery report checksum differs')
    report = ctl.read(report_path)
    with sb.offline(root):
        state = sb.restore_state(root)
        # Only an exact completed transition can be replayed. No marker reset.
        if state and state.get('failed_provision_recovery'):
            previous = ctl.bound_record(state['failed_provision_recovery'])
            if (not (root/WITNESS).exists() and (root/'media-kit/incomplete').is_file()
                    and previous['report'] == report and previous['report_sha256'] == sha256 and {k:v for k,v in state.items() if k != 'failed_provision_recovery'} == previous['quarantine_state']):
                pr.validate_final(root, state, expected_runtime=ctl.bound_record(state['refresh'])['runtime'])
                return {'recovered': True, 'replayed': True, 'quarantined': True}
            raise ValueError('different recovery or progressed quarantine; no replay')
        state = validate(root, report)
        # Preserve the COMPLETE previous marker and activation, not just IDs.
        quarantined = dict(state)
        quarantined.update(reconciled=False, preserved_holds=state['held_admission'])
        quarantined.pop('held_admission')
        value = {'format': FORMAT, 'report': report, 'report_sha256': sha256,
                 'previous_state': state, 'quarantine_state': quarantined}
        ref = ctl.record(root, 'failed-provision-recovery', value)
        quarantined['failed_provision_recovery'] = ref
        # Do not put self-reference inside the record. Replay compares this shape.
        ctl.atomic(root/sb.MARKER, quarantined)
        return {'recovered': True, 'quarantined': True, 'receipt': ref,
                'partial_media_preserved': True, 'fresh_refresh_required': True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path); p.add_argument('report', type=Path)
    p.add_argument('--sha256', required=True)
    a = p.parse_args()
    import os
    os.umask(0o077)
    try:
        from .repository import dump
        print(dump(recover(a.root, a.report, a.sha256)))
    except (ValueError, OSError, KeyError) as exc:
        p.exit(1, 'Failed-provision recovery refused: '+str(exc)+'\n')


if __name__ == '__main__': main()
