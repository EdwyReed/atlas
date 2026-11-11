"""Explicit failed-intent recovery; no rewrites of historical jobs or CAS heads."""
from .contracts import Fault
from .corpus import entity_id, point_id
from .repository import digest


def source_revisions(o):
    """Prove source publication from exact frozen parent + child + entry token.

    A matching editor/attribution or revision number alone is not ownership.
    Include writes before stage/parent ACK, not just acknowledged receipts.
    """
    children = {(j['owner_principal'], j['payload'].get('request_id')): j
                for j in o.e.repo.iter_jobs('corpus')}
    proven = {}
    for parent in o.e.repo.iter_jobs('onboarding'):
        if not parent.get('verified'):
            continue
        payloads = parent.get('corpus_payloads', [])
        if parent.get('corpus_payload'):
            payloads = [*payloads, parent['corpus_payload']]
        for payload in payloads:
            child = children.get((parent['owner_principal'], payload['request_id']))
            if not child or child['payload'] != {'operation': 'corpus.import', **payload}:
                continue
            if child.get('corpus_operation') != 'corpus.import':
                continue
            for index, entry in enumerate(payload['entries']):
                c = entry['collection']
                eid = entity_id(c, entry['identity'])
                rid = point_id(['revision', c, eid, [child['id'], index]])
                proven[rid] = {
                    'observation_id': digest({k: entry[k] for k in
                        ('collection', 'identity', 'body', 'attribution', 'occurrences')}),
                    'editor': {'principal': child['owner_principal'], 'source_client': child['source_client']},
                    'parent_job_id': parent['id'], 'child_job_id': child['id']}
    return proven


def validate_entries(o, job, entries, *, captured=True):
    """Never auto-rebase an external edit, even if it predates recovery reserve."""
    proven = source_revisions(o)
    evidence = []
    for entry in entries:
        c = entry['collection']
        if c not in ('loras', 'checkpoints', 'generations'):
            continue
        eid = entity_id(c, entry['identity'])
        revisions = o.e.corpus.revisions(c, eid)
        actual = revisions[-1]['revision'] if revisions else 0
        if captured and actual != entry['expected_revision']:
            raise Fault('REVISION_CONFLICT', 'head changed since recovery intent was reserved')
        for revision in revisions:
            proof = proven.get(revision['revision_id'])
            if (not proof or revision['action'] != 'put' or revision['deleted']
                    or revision['observation_id'] != proof['observation_id']
                    or revision['editor'] != proof['editor']):
                raise Fault('RECOVERY_EXTERNAL_EDIT', 'recovery refuses non-onboarding history; explicit reconciliation required')
        if revisions:
            evidence.append({'collection': c, 'entity_id': eid,
                             'revision_id': revisions[-1]['revision_id'], 'revision': actual,
                             **proven[revisions[-1]['revision_id']]})
    return evidence
