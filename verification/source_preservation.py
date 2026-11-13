"""Focused disposable SQLite source-preservation counterexamples; no servers."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import runpy
import sqlite3

helpers = runpy.run_path(str(Path(__file__).with_name('connection_restore.py')))
snapshot, changes, files = (helpers[n] for n in ('registry_snapshot', 'preservation_changes', 'files'))
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--out', type=Path, required=True)
a = p.parse_args()
a.out.mkdir(parents=True, exist_ok=False, mode=0o700)
root = a.out.absolute()/'source'
root.mkdir()
with closing(sqlite3.connect(root/'registry.sqlite3')) as db:
    db.execute('pragma journal_mode=WAL')
    db.execute('create table opaque(k text, body blob)')
    db.execute('insert into opaque values(?,?)', ('initial', b'\x00\xff'))
    db.commit()
# Closed, checkpointed WAL-mode main DB: immutable read must not create sidecars.
before = files(root)
first = snapshot(root, before)
assert files(root) == before and first['read_mode']=='immutable_closed_no_pending_journal'
# This is the observed SQLite mode=ro side effect, not a product write.
with closing(sqlite3.connect((root/'registry.sqlite3').as_uri()+'?mode=ro', uri=True)) as reader:
    assert reader.execute('select count(*) from opaque').fetchone()==(1,)
after = files(root)
all_changes, runtime, durable = changes(before, after)
assert runtime and not durable
assert snapshot(root, after)['sha256']==first['sha256']
checks = ['closed immutable snapshot: full schema/rows, no sidecars',
          'real read-only WAL-mode read: SHM/empty-WAL effects reported, durable bytes unchanged']
# Open writer is only a disposable counterexample: committed WAL data MUST be
# read, never hidden by immutable=1. Main acceptance uses both owner locks.
with closing(sqlite3.connect(root/'registry.sqlite3')) as db:
    db.execute('pragma wal_autocheckpoint=0')
    db.execute('insert into opaque values(?,?)', ('committed-WAL', b'\x01'))
    db.commit()
    wal_inventory = files(root)
    assert wal_inventory['registry.sqlite3-wal']['bytes']>0
    wal_snapshot = snapshot(root, wal_inventory)
    assert wal_snapshot['read_mode']=='read_only_including_WAL'
    assert 'committed-WAL' in wal_snapshot['schema_and_rows'] and wal_snapshot['sha256']!=first['sha256']
    for label, altered in [('missing', {k:v for k,v in wal_inventory.items() if k!='registry.sqlite3-wal'}),
                           ('empty', {**wal_inventory, 'registry.sqlite3-wal': {'bytes':0,'sha256':'empty'}}),
                           ('changed', {**wal_inventory, 'registry.sqlite3-wal': {**wal_inventory['registry.sqlite3-wal'],'sha256':'changed'}})]:
        assert 'registry.sqlite3-wal' in changes(wal_inventory, altered)[2], label
checks.append('nonempty WAL included logically; missing/empty/changed nonempty WAL rejected')
for name in ('registry.sqlite3', 'artifacts/original.png', 'corpus/qdrant/collection/tags/storage.sqlite', 'corpus/qdrant/collection/tags/storage.sqlite-shm'):
    old={name:{'bytes':17,'sha256':'old'}}
    new={name:{'bytes':17,'sha256':'new'}}
    assert name in changes(old,new)[2]
checks.append('main DB/artifacts/Qdrant bytes (including Qdrant sidecars) remain strict')
receipt={'ok':True,'checks':checks,'actual_read_only_sidecar_changes':all_changes,'byte_zero_writes_claimed':False}
(a.out/'result.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt,indent=2))
