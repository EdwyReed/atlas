"""Standalone host for the original Atlas plugin on the existing Engine."""
import asyncio
import base64
import io
import json
import re
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent / 'public_assets'


def attach(original, engine):
    from .server import engine_call

    async def invoke(request, op, args):
        identity = request.scope['atlas_identity']
        return await engine_call(engine, op, args, identity['principal'], identity['source'])

    async def operation(request):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                return JSONResponse({'detail': 'body too large'}, 413)
            chunks.append(chunk)
        try:
            body = json.loads(b''.join(chunks))
            if not isinstance(body, dict) or set(body) - {'operation', 'arguments'}:
                raise ValueError()
            op, args = body['operation'], body.get('arguments', {})
            if not isinstance(op, str) or not isinstance(args, dict):
                raise ValueError()
        except (KeyError, ValueError, UnicodeDecodeError):
            return JSONResponse({'detail': 'operation and arguments required'}, 422)
        result = await invoke(request, op, args)
        if not result.get('ok'):
            return JSONResponse({'detail': result.get('error')}, 409)
        return JSONResponse(result['result'])

    async def media(request):
        sha = request.path_params['sha']
        if not re.fullmatch('[a-f0-9]{64}', sha):
            return JSONResponse({'detail': 'Invalid digest'}, 422)
        raw = bytearray()
        for offset in range(0, 8*1024*1024, 32768):
            result = await invoke(request, 'corpus.files.read', {'sha256':sha, 'offset':offset, 'size':32768})
            if not result.get('ok'):
                return JSONResponse({'detail':result.get('error')}, 409)
            r = result['result']
            chunk = base64.b64decode(r['base64'], validate=True)
            raw.extend(chunk)
            if offset == 0 and chunk[4:8] == b'ftyp':
                return JSONResponse({'status':'unsupported', 'media_type':'video/mp4', 'reason':'video_preview_unavailable', 'sha256':sha})
            if len(raw) >= r['size']:
                break
        else:
            return JSONResponse({'detail':'Preview exceeds 8 MiB'}, 413)
        def thumbnail():
            from PIL import Image, UnidentifiedImageError
            try:
                with Image.open(io.BytesIO(raw)) as im:
                    im.thumbnail((640,640))
                    out = io.BytesIO()
                    im.convert('RGB').save(out, 'JPEG', quality=85)
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                return JSONResponse({'detail':'Saved media cannot be decoded as an image'}, 415)
            return JSONResponse({'data_url':'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()})
        return await asyncio.to_thread(thumbnail)

    async def index(request):
        return FileResponse(ROOT/'index.html', headers={'Cache-Control':'no-cache'})

    async def root(request):
        return RedirectResponse('/atlas/')

    public = Starlette(routes=[
        Route('/', root), Route('/atlas', index), Route('/atlas/', index),
        Route('/atlas/api/operation', operation, methods=['POST']),
        Route('/atlas/api/media/{sha}', media),
        Mount('/atlas/assets', app=StaticFiles(directory=ROOT)),
    ])

    class Surface:
        async def __call__(self, scope, receive, send):
            path = scope.get('path', '')
            if scope['type'] == 'http':
                if path == '/atlas/v1/schema' or path.startswith('/atlas/v1/ops/') or path == '/atlas/mcp' or path.startswith('/atlas/mcp/'):
                    inner = dict(scope, path=path[len('/atlas'):])
                    inner['raw_path'] = inner['path'].encode()
                    return await original(inner, receive, send)
                if path in ('/', '/atlas') or path.startswith('/atlas/'):
                    return await public(scope, receive, send)
            # One lifespan, one existing Engine and worker/corpus owner.
            return await original(scope, receive, send)
    return Surface()
