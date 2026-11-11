"""Optional existing BGE text space; encoder only, no generative LLM or download fallback."""
import functools,threading,math
from pathlib import Path
from .contracts import Fault
class TextEncoder:
    model_name='BAAI/bge-small-en-v1.5'
    def __init__(self,cache):self.cache=Path(cache);self.model=None;self.lock=threading.Lock()
    @functools.lru_cache(maxsize=128)
    def encode(self,text):
        with self.lock:
            try:
                if self.model is None:
                    from fastembed import TextEmbedding
                    self.model=TextEmbedding(self.model_name,cache_dir=str(self.cache),local_files_only=True,threads=2,providers=['CPUExecutionProvider'])
                vector=next(self.model.embed([text])).tolist()
                if len(vector)!=384 or not all(math.isfinite(x) for x in vector) or not any(vector):raise ValueError('invalid vector')
                return vector
            except Exception as e:raise Fault('SEMANTIC_UNAVAILABLE','local BGE encoder failed; no model download or literal fallback') from e
