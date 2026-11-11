"""Loopback HTTP + Streamable HTTP MCP adapters over one Engine instance."""
import argparse,asyncio,contextlib,fcntl,hmac,json,os
from pathlib import Path
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route,Mount
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool,TextContent,CallToolResult
from .contracts import SCHEMAS
from .engine import Engine
from .upstream import Invoke,Atlas

async def engine_call(engine, op, arguments, principal, source):
    if op not in ('search', 'evidence'):
        return await asyncio.to_thread(engine.call, op, arguments, principal, source)
    import threading
    import time
    from .reference_reader import read_budget, READ_SECONDS
    canceled = threading.Event()
    deadline = time.monotonic() + READ_SECONDS
    def work():
        from .contracts import Fault
        try:
            with read_budget(deadline, lambda: canceled.is_set() or engine.stop.is_set()):
                return engine.call(op, arguments, principal, source)
        except Fault as e:
            return {'ok':False,'operation':op,'error':{'code':e.code,'message':str(e)}}
    try:
        return await asyncio.wait_for(asyncio.to_thread(work), READ_SECONDS + 1)
    except asyncio.TimeoutError:
        return {'ok':False,'operation':op,'error':{'code':'TIMEOUT','message':'reference read deadline exceeded'}}
    finally:
        # Canceling the async waiter alone does not stop its worker thread.
        canceled.set()

class Auth:
    def __init__(self,app,clients,port,host='127.0.0.1',container_endpoint=None):
        self.app=app;self.clients=clients
        address='['+host+']' if ':' in host else host
        self.hosts={address+':'+str(port),'localhost:'+str(port)}
        if container_endpoint is not None:
            self.hosts={container_endpoint.removeprefix('http://')}
        self.origins={'http://'+h for h in self.hosts}
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        pairs=scope.get('headers',[]);headers=dict(pairs)
        if any(sum(k==name for k,_ in pairs)>1 for name in (b'host',b'origin',b'authorization')):
            return await JSONResponse({'error':'duplicate security header'},400)(scope,receive,send)
        if (headers.get(b'host',b'').decode('latin1').lower() not in self.hosts or
                (b'origin' in headers and headers[b'origin'].decode('latin1') not in self.origins)):
            return await JSONResponse({'error':'untrusted Host or Origin'},403)(scope,receive,send)
        auth=headers.get(b'authorization',b'')
        identity=next((c for c in self.clients if hmac.compare_digest(auth,('Bearer '+c['token']).encode())),None)
        if identity is None:
            return await JSONResponse({'error':'authentication required'},401,headers={'WWW-Authenticate':'Bearer'})(scope,receive,send)
        scope['atlas_identity']=identity
        await self.app(scope,receive,send)

def make_app(engine,clients,port,host='127.0.0.1',container_endpoint=None):
    mcp=Server('atlas-engine')
    @mcp.list_tools()
    async def list_tools():
        return [Tool(name=op.replace('.','_'),description='Atlas Engine '+op+'; same contract as HTTP /v1/ops/'+op,inputSchema=s.model_json_schema()) for op,s in SCHEMAS.items()]
    @mcp.call_tool(validate_input=False)
    async def call_tool(name,arguments):
        ctx=mcp.request_context;identity=ctx.request.scope['atlas_identity']
        op=next((o for o in SCHEMAS if o.replace('.','_')==name),name)
        result=await engine_call(engine,op,arguments,identity['principal'],identity['source'])
        return CallToolResult(content=[TextContent(type='text',text=json.dumps(result,ensure_ascii=False))],structuredContent=result,isError=not result['ok'])
    from mcp.server.transport_security import TransportSecuritySettings
    manager=StreamableHTTPSessionManager(mcp,json_response=True,stateless=True,max_request_body_size=65536,security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    async def ops(request:Request):
        chunks=[];size=0
        async for chunk in request.stream():
            size+=len(chunk)
            if size>65536:return JSONResponse({'error':'body too large'},413)
            chunks.append(chunk)
        try:
            p=json.loads(b''.join(chunks));
            if not isinstance(p,dict):raise ValueError()
        except (ValueError,UnicodeDecodeError):return JSONResponse({'error':'JSON object required'},400)
        identity=request.scope['atlas_identity']
        r=await engine_call(engine,request.path_params['operation'],p,identity['principal'],identity['source'])
        return JSONResponse(r)
    async def schema(request):return JSONResponse({k:v.model_json_schema() for k,v in SCHEMAS.items()})
    async def mcp_app(scope,receive,send):await manager.handle_request(scope,receive,send)
    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            engine.start()
            async with manager.run():yield
        finally:await asyncio.to_thread(engine.close)
    app=Starlette(routes=[Route('/v1/ops/{operation}',ops,methods=['POST']),Route('/v1/schema',schema),Mount('/mcp',app=mcp_app)],lifespan=lifespan)
    return Auth(app,clients,port,host,container_endpoint)
def main():
    from .config import Config, clients, secret, bind_connection
    from .contracts import Fault
    from .upstream import unconfigured
    p=argparse.ArgumentParser(description='Atlas standalone headless daemon')
    p.add_argument('--config',required=True,type=Path)
    p.add_argument('--root',type=Path,help='explicit isolated state root override')
    p.add_argument('--host',help='loopback IP override')
    p.add_argument('--port',type=int)
    a=p.parse_args()
    try:
        config=Config.load(a.config,root=a.root,host=a.host,port=a.port)
        identities=clients(config.clients_file)
        connection=config.connections[0] if config.connections else None
        invoke=Invoke(connection.url,secret(connection.token_file)) if connection else unconfigured(Invoke,'INVOKE_UNCONFIGURED')
        if config.reference_delta:
            from .stream_delta import StreamReferenceMirror
            from .scoped_delta import ScopedReferenceMirror
            cls=StreamReferenceMirror if json.loads((config.reference_delta/'receipt.json').read_text()).get('format')=='atlas-stream-delta-v1' else ScopedReferenceMirror
            atlas=cls(config.reference_mirror,config.reference_delta)
        elif config.reference_mirror:
            from .reference_mirror import ReferenceMirror
            atlas=ReferenceMirror(config.reference_mirror)
        elif config.reference_collections is not None:
            from .typed_reference import TypedReference
            atlas=TypedReference(config.reference_url,config.reference_collections,secret(config.reference_token_file))
        else:
            atlas=Atlas(config.reference_url,token=secret(config.reference_token_file)) if config.reference_url else unconfigured(Atlas,'REFERENCE_UNCONFIGURED')
        media=json.loads(config.media_config_file.read_text()) if config.media_config_file else None
        config.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (config.root/'service.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise Fault('STATE_BUSY','another daemon owns this state root')
            bind_connection(config.root,connection)
            from .projection import Projection
            from .state_backup import restore_state
            restored=restore_state(config.root)
            quarantined=bool(restored and not restored['reconciled'])
            projection=Projection(config.projection_dir) if config.projection_dir and not quarantined else None
            policy=json.loads(config.policy_file.read_text()) if config.policy_file else None
            writes=bool(connection and connection.enable_writes)
            engine=Engine(config.root,config.vocabulary,invoke,atlas,writes,policy,
                          native_events=bool(connection),allow_admin=writes,
                          projection=projection,corpus_root=config.root/'corpus',
                          civitai_url=config.civitai_url,media_config=media)
            engine.startup_mode='standalone'
            engine.connection_id=connection.id if connection else None
            engine.backend_generation=connection.backend_generation if connection else None
            uvicorn.run(make_app(engine,identities,config.port,config.host,config.container_endpoint),host=config.host,port=config.port,log_level='warning',access_log=False)
    except (OSError,ValueError,Fault) as e:
        p.exit(1,'Atlas startup refused: '+getattr(e,'code',type(e).__name__)+': '+str(e)+'\n')
if __name__=='__main__':main()
