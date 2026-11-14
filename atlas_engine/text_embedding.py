"""Optional local BGE encoder; no generative LLM or download fallback."""
import functools
import hashlib
import math
import threading
from pathlib import Path
from .contracts import Fault


class TextEncoder:
    model_name = 'BAAI/bge-small-en-v1.5'

    def __init__(self, cache):
        self.cache = Path(cache)
        self.model = None
        self.lock = threading.Lock()
        self.fingerprint = None

    def model_pin(self):
        root = self.cache/'models--qdrant--bge-small-en-v1.5-onnx-q'
        # Pin content, not host paths or mutable download metadata. Fastembed's
        # local snapshot files can be symlinks to immutable cache blobs.
        files = sorted(p for p in (root/'snapshots').glob('*/*') if p.is_file())
        if not files or not any(p.suffix == '.onnx' for p in files):
            raise ValueError('local BGE snapshot required')
        h = hashlib.sha256()
        for p in files:
            h.update(str(p.relative_to(root)).encode()+b'\0')
            with p.open('rb') as f:
                for raw in iter(lambda: f.read(1024*1024), b''):
                    h.update(raw)
        return h.hexdigest()

    @staticmethod
    def validate(vector):
        if len(vector) != 384 or not all(math.isfinite(x) for x in vector) or not any(vector):
            raise ValueError('invalid vector')

    @functools.lru_cache(maxsize=128)
    def encode(self, text):
        return self.encode_batch([text])[0]

    def encode_batch(self, texts):
        if not texts:
            return []
        with self.lock:
            try:
                if self.model is None:
                    from fastembed import TextEmbedding
                    before = self.model_pin()
                    model = TextEmbedding(self.model_name, cache_dir=str(self.cache), local_files_only=True,
                                          threads=2, providers=['CPUExecutionProvider'])
                    if self.model_pin() != before:
                        raise ValueError('model changed during load')
                    self.model = model
                    self.fingerprint = before
                vectors = [v.tolist() for v in self.model.embed(texts, batch_size=64)]
                if len(vectors) != len(texts):
                    raise ValueError('encoder batch length differs')
                for vector in vectors:
                    self.validate(vector)
                return vectors
            except Exception as e:
                raise Fault('SEMANTIC_UNAVAILABLE', 'local BGE encoder failed; no model download or literal fallback') from e
