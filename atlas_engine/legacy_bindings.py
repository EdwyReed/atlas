"""Offline copy-only owner migration for retained quarantine (no source reimport).

Default is inventory, never inferred owners. Apply requires a complete explicit
mapping, the exact sealed ledger hash, and a NEW destination. It does not activate
that destination, adopt native handles, drain outbox, or establish a writer fence.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .legacy_compat import bind
from .repository import Repository, digest

FORMAT = 'atlas-legacy-owner-mapping-v1'


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def plain(path):
    p = Path(path).absolute()
    if any(q.is_symlink() for q in (p, *p.parents)):
        raise ValueError('linked path refused')
    return p


def sealed_ledger(path):
    p = plain(path)
    if not p.is_file() or p.stat().st_nlink != 1:
        raise ValueError('regular unlinked ledger required')
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(p) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('sealed ledger required; nonempty SQLite sidecar')
    return p


def inventory(c):
    rows = c.execute('select source,table_name,row_number,sha256,data from legacy_rows order by source,table_name,row_number').fetchall()
    sources = {}
    for source, table, number, stored, data in rows:
        if hashlib.sha256(data.encode()).hexdigest() != stored:
            raise ValueError('quarantine hash mismatch')
        group = sources.setdefault(source, {})
        sequence = group.setdefault(table, [])
        if number != len(sequence) + 1:
            raise ValueError('quarantine row sequence mismatch')
        sequence.append([number, stored, data])
    return [{'source': source, 'quarantine_sha256': digest(tables),
             'tables': {table: len(rows) for table, rows in tables.items()},
             'requires_binding': bool(tables.get('requests'))}
            for source, tables in sorted(sources.items())]


def validate_mapping(mapping, ledger_sha, entries):
    if not isinstance(mapping, dict) or set(mapping) != {'format', 'ledger_sha256', 'bindings'} or mapping['format'] != FORMAT:
        raise ValueError('explicit owner mapping contract required')
    if mapping['ledger_sha256'] != ledger_sha:
        raise ValueError('ledger pin mismatch')
    if not isinstance(mapping['bindings'], list) or not mapping['bindings']:
        raise ValueError('empty mapping cannot release unbound history')
    expected = {e['source']: e for e in entries if e['requires_binding']}
    seen, namespaces = set(), set()
    for b in mapping['bindings']:
        if not isinstance(b, dict) or set(b) != {'source', 'quarantine_sha256', 'namespace', 'owner_principal', 'project'}:
            raise ValueError('all binding fields must be explicit, including nullable project')
        if any(not isinstance(b[k], str) or not b[k].strip() for k in ('source', 'namespace', 'owner_principal')):
            raise ValueError('nonempty source/namespace/principal required')
        if b['project'] is not None and (not isinstance(b['project'], str) or not b['project'].strip()):
            raise ValueError('project must be null or nonempty string')
        if b['source'] not in expected or b['source'] in seen or b['namespace'] in namespaces:
            raise ValueError('unknown/duplicate source or namespace')
        if not isinstance(b['quarantine_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', b['quarantine_sha256']) or b['quarantine_sha256'] != expected[b['source']]['quarantine_sha256']:
            raise ValueError('quarantine pin mismatch')
        seen.add(b['source']); namespaces.add(b['namespace'])
    if seen != set(expected):
        raise ValueError('mapping must cover every request source; partial release refused')


def run(ledger, mapping=None, destination=None):
    source = sealed_ledger(ledger)
    before = sha(source)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as c:
        c.execute('BEGIN')
        entries = inventory(c)
        result = {'format': 'atlas-legacy-owner-migration-receipt-v1', 'source_ledger': str(source),
                  'source_ledger_sha256': before, 'inventory': entries,
                  'owners_inferred': False, 'activation': False, 'source_writes': False,
                  'native_handle_adoption': False, 'replay': False, 'outbox_drain': False,
                  'mode': 'inventory', 'bindings_applied': 0}
        if mapping is not None:
            validate_mapping(mapping, before, entries)
            result['mapping_sha256'] = digest(mapping)
            result['mode'] = 'validated-dry-run'
        if destination is not None:
            if mapping is None:
                raise ValueError('apply requires reviewed explicit mapping')
            target = plain(destination)
            if target == source.parent or target in source.parents or source.parent in target.parents:
                raise ValueError('destination overlaps sealed source')
            target.mkdir(parents=True, mode=0o700, exist_ok=False)
            path = target / 'registry.sqlite3'
            with closing(sqlite3.connect(path)) as d:
                c.backup(d)
                if d.execute('pragma integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('backup integrity failure')
            path.chmod(0o600)
            repo = Repository(target)
            with closing(repo.connect()) as d:
                try:
                    d.execute('BEGIN IMMEDIATE')
                    if inventory(d) != entries:
                        raise ValueError('copy quarantine mismatch')
                    reservations = []
                    for b in mapping['bindings']:
                        n = bind(d, b['source'], b['namespace'], b['owner_principal'], b['project'], b['quarantine_sha256'])
                        reservations.append({'namespace': b['namespace'], 'reservations': n})
                    if sha(source) != before:
                        raise ValueError('source drift during migration')
                    d.commit()
                except BaseException:
                    d.rollback()
                    raise
            # Read back the exact target after the committed write.
            with closing(repo.connect()) as d:
                actual = [list(r) for r in d.execute('select * from legacy_namespaces order by namespace')]
                wanted = sorted([b['namespace'], b['source'], b['owner_principal'], b['project'], b['quarantine_sha256']] for b in mapping['bindings'])
                if actual != wanted or inventory(d) != entries:
                    raise ValueError('binding/quarantine readback mismatch')
            result.update(mode='copy-applied-not-activated', destination=str(target),
                          bindings_applied=len(reservations), reservations=reservations)
    if sha(source) != before:
        raise ValueError('source drift')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger', required=True)
    p.add_argument('--mapping', help='operator-reviewed JSON; never generated from profile names')
    p.add_argument('--destination', help='NEW copy only; omission is dry-run')
    p.add_argument('--receipt', required=True)
    a = p.parse_args()
    os.umask(0o077)
    # Reserve receipt before any copy/write so an existing receipt fails early.
    with open(a.receipt, 'x', encoding='utf-8') as f:
        try:
            mapping = json.loads(Path(a.mapping).read_text()) if a.mapping else None
            result = run(a.ledger, mapping, a.destination)
        except BaseException as e:
            json.dump({'ok': False, 'error': str(e), 'destination_not_activated': True}, f, indent=2)
            raise
        json.dump({'ok': True, **result}, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
