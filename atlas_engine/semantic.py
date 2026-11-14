"""Disposable canonical dense index; never reuse unqualified legacy vectors.

Qdrant payload heads remain authoritative. Content-addressed vector files are a
verified, removable CPU cache, not a second corpus or operational ledger.
"""
import hashlib
import os
from pathlib import Path
import struct
import threading
from .contracts import Fault
from .corpus import COLLECTIONS, point_id
from .repository import dump, digest


def checkpoint():
    from .reference_reader import _budget
    budget = _budget.get()
    if budget is not None:
        budget.check()


class CanonicalSemantic:
    def __init__(self, corpus, encoder):
        self.corpus = corpus
        self.encoder = encoder
        self.lock = threading.Lock()
        self.client = None
        self.signature = None
        self.cache = Path(encoder.cache).parent / 'canonical-vectors'

    def close(self):
        with self.lock:
            if self.client is not None:
                self.client.close()
                self.client = None

    def snapshot(self):
        rows = []
        with self.corpus.lock:
            for collection in COLLECTIONS:
                heads = self.corpus.scan(collection, kind='head')
                for start in range(0, len(heads), 128):
                    checkpoint()
                    batch = heads[start:start+128]
                    records = {r.id: r.payload for r in self.corpus.client.retrieve(
                        collection, [h['revision_id'] for h in batch], with_payload=True, with_vectors=False)}
                    for head in batch:
                        record = records.get(head['revision_id'])
                        if record is None:
                            raise Fault('CORPUS_INTEGRITY', 'stored head revision missing')
                        if not record['deleted']:
                            text = dump({'collection': collection, 'identity': record['identity'], 'body': record['body']})
                            rows.append((collection, record, text))
        return sorted(rows, key=lambda r: (r[0], r[1]['entity_id']))

    def cached(self, key):
        try:
            raw = (self.cache/key[:2]/key).read_bytes()
            if len(raw) != 384*8+32 or hashlib.sha256(raw[32:]).digest() != raw[:32]:
                return None
            vector = list(struct.unpack('<384d', raw[32:]))
            self.encoder.validate(vector)
            return vector
        except (OSError, ValueError, struct.error):
            return None  # Corrupt/missing derived cache is safely recomputed.

    def save(self, key, vector):
        folder = self.cache/key[:2]
        raw = struct.pack('<384d', *vector)
        tmp = folder/(key+'.pending')
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tmp.open('wb') as f:
                f.write(hashlib.sha256(raw).digest()+raw)
                f.flush()
            os.replace(tmp, folder/key)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass  # A read-only/unwritable derived cache does not disable search.

    def search(self, query, limit):
        from qdrant_client import QdrantClient
        from qdrant_client.models import VectorParams, Distance, PointStruct
        with self.lock:
            vector = self.encoder.encode(query)  # Missing local weights fails explicitly.
            rows = self.snapshot()
            signature = digest([(c, r['revision_id'], digest(text)) for c, r, text in rows])
            if signature != self.signature or self.client is None:
                candidate = QdrantClient(location=':memory:')
                try:
                    candidate.create_collection('canonical_dense', vectors_config=VectorParams(size=384, distance=Distance.COSINE))
                    for start in range(0, len(rows), 64):
                        checkpoint()
                        batch = rows[start:start+64]
                        keys = [digest([self.encoder.model_name, self.encoder.fingerprint, text]) for _, _, text in batch]
                        vectors = [self.cached(key) for key in keys]
                        missing = [i for i, value in enumerate(vectors) if value is None]
                        for i, encoded in zip(missing, self.encoder.encode_batch([batch[i][2] for i in missing]), strict=True):
                            vectors[i] = encoded
                            self.save(keys[i], encoded)
                        checkpoint()
                        points = [PointStruct(id=point_id([c, r['entity_id']]), vector=v, payload={'collection':c, 'record':r})
                                  for (c, r, _), v in zip(batch, vectors, strict=True)]
                        candidate.upsert('canonical_dense', points, wait=True)
                except BaseException:
                    candidate.close()
                    raise
                if self.client is not None:
                    self.client.close()
                self.client = candidate
                self.signature = signature
            matches = self.client.query_points('canonical_dense', query=vector, limit=limit, with_payload=True).points
            return {'items': [{'collection': m.payload['collection'], 'record': m.payload['record'], 'score': m.score} for m in matches],
                'complete': True, 'indexed': len(rows), 'snapshot_sha256': signature,
                'embedding_space': self.encoder.model_name, 'model_files_sha256': self.encoder.fingerprint,
                'scope': 'stored canonical heads; legacy vectors excluded',
                'projection': 'literal JSON collection/identity/body; encoder token truncation applies',
                'storage': 'disposable in-memory Qdrant; verified content-addressed vector cache',
                'score': 'cosine; not comparable to literal or legacy scores'}
