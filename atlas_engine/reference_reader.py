"""Bounded reference reads: indexed identity probes, streaming rows, finite work.

Layer priority is newest first. Any newer raw point (even a non-reference or
private entity) shadows an older identity; a hold shadows its own layer too.
No corpus-sized Python identity set or cross-layer payload list is built.
"""
import contextlib
import contextvars
import sqlite3
import time

from .contracts import Fault

_budget = contextvars.ContextVar('reference_read_budget', default=None)
READ_SECONDS = 15.0


class Budget:
    def __init__(self, deadline=None, cancel=lambda: False):
        self.deadline = time.monotonic() + READ_SECONDS if deadline is None else deadline
        self.cancel = cancel

    def check(self):
        if self.cancel():
            raise Fault('CANCELED', 'reference read canceled')
        if time.monotonic() >= self.deadline:
            raise Fault('TIMEOUT', 'reference read deadline exceeded')

    def interrupted(self):
        return self.cancel() or time.monotonic() >= self.deadline


@contextlib.contextmanager
def read_budget(deadline=None, cancel=lambda: False):
    budget = Budget(deadline, cancel)
    token = _budget.set(budget)
    try:
        budget.check()
        yield budget
    finally:
        _budget.reset(token)


@contextlib.contextmanager
def connection(reader, budget):
    budget.check()
    with contextlib.closing(reader.connect()) as db:
        db.execute('pragma cache_size=-256')
        db.execute('pragma temp_store=FILE')
        db.set_progress_handler(budget.interrupted, 1000)
        try:
            yield db
        except sqlite3.OperationalError:
            budget.check()
            raise
        finally:
            db.set_progress_handler(None, 0)


class DiskKeys:
    """Set-like membership only; no enumeration or cache growing with corpus size."""
    def __init__(self, path, table='points', extra=(), where=None, sealed_attachment=False):
        self.path = path
        self.table = table
        self.extra = extra
        self.where = where
        self.sealed_attachment = sealed_attachment

    def connect(self):
        from pathlib import Path
        if self.sealed_attachment:
            from .attachment import immutable
            return immutable(self.path)
        return sqlite3.connect(Path(self.path).absolute().as_uri() + '?mode=ro', uri=True)

    def bind(self, stack, budget):
        db = stack.enter_context(connection(self, budget))
        sql = 'select 1 from ' + self.table + ' where collection=? and id=?'
        if self.where:sql += ' and (' + self.where + ')'
        extra = bind_keys(self.extra, stack, budget)
        def contains(key):
            budget.check()
            return extra(key) or db.execute(sql, key).fetchone() is not None
        return contains

    def __contains__(self, key):
        budget = _budget.get() or Budget()
        with contextlib.ExitStack() as stack:
            return self.bind(stack, budget)(key)


def bind_keys(keys, stack, budget):
    return keys.bind(stack, budget) if isinstance(keys, DiskKeys) else keys.__contains__


def layer_specs(reader):
    if hasattr(reader, 'overlay'):
        yield reader.overlay, reader.holds
        yield from layer_specs(reader.base)
    else:
        yield reader, ()


def iter_items(reader, sql, args=(), limit=None):
    """Yield in the original layer/order sequence; limit counts public survivors."""
    budget = _budget.get() or Budget()
    if limit is not None and limit <= 0:
        return
    with contextlib.ExitStack() as stack:
        newer = []
        count = 0
        for layer, holds in layer_specs(reader):
            budget.check()
            own_hold = bind_keys(holds, stack, budget)
            with connection(layer, budget) as db:
                for row in db.execute(sql, args):
                    budget.check()
                    key = (row[0], row[1])
                    if own_hold(key) or any(shadows(key) for shadows in newer):
                        continue
                    item = layer.unpack(row)
                    yield item
                    count += 1
                    if limit is not None and count >= limit:
                        return
            # Only layers with older candidates need shadow probes. Never scan IDs.
            covered = DiskKeys(layer.path, extra=holds, sealed_attachment=getattr(layer, 'sealed_attachment', False))
            newer.append(covered.bind(stack, budget))


RESOURCE_SQL = "select collection,id,payload from entities where entity_type='resource' order by collection,id"
EVIDENCE_SQL = ('select e.collection,e.id,e.payload from reference_keys k join entities e using(collection,id) '
                'where k.kind=? and k.key=? order by e.collection,e.id')


def search_resources(reader, query, limit):
    if limit <= 0:
        return []
    result = []
    rows = iter_items(reader, RESOURCE_SQL)
    try:
        for item in rows:
            if query.lower() in str(item['payload']).lower():
                result.append(item)
                if len(result) >= limit:
                    break
    finally:
        rows.close()
    return result
