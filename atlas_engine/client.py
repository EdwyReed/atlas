"""Installed thin HTTP CLI. Standard library only, no engine or upstream imports."""
import argparse,json,ipaddress
from pathlib import Path
from urllib.parse import quote,urlsplit
from urllib.request import Request,build_opener,HTTPRedirectHandler,ProxyHandler

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def call(operation,arguments,config):
    base=config.get('endpoint','http://127.0.0.1:12088').rstrip('/')
    u=urlsplit(base)
    try:loopback=u.hostname=='localhost' or ipaddress.ip_address(u.hostname).is_loopback
    except ValueError:loopback=False
    if u.scheme!='http' or not loopback or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):
        return {'ok':False,'error':{'code':'CLIENT_CONFIG','message':'loopback HTTP endpoint required'}}
    try:
        path=Path(config['token_file'])
        if path.stat().st_mode&0o077:raise ValueError('token file must be private')
        token=path.read_text().strip()
        if len(token)<32:raise ValueError('invalid token')
        body=json.dumps(arguments).encode()
        if len(body)>65536:raise ValueError('arguments exceed transport limit')
        req=Request(base+'/v1/ops/'+quote(operation,safe='.'),data=body,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
        with build_opener(ProxyHandler({}),NoRedirect).open(req,timeout=45) as r:
            raw=r.read(16*1024*1024+1)
            if len(raw)>16*1024*1024:raise ValueError('response exceeds client limit')
            result=json.loads(raw)
            if not isinstance(result,dict) or type(result.get('ok')) is not bool:raise ValueError('invalid service response')
            return result
    except (OSError,ValueError,KeyError) as e:return {'ok':False,'error':{'code':'CLIENT_UNAVAILABLE','message':type(e).__name__}}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation');p.add_argument('--arguments',default='{}');p.add_argument('--endpoint',default='http://127.0.0.1:12088');p.add_argument('--token-file',required=True)
    a=p.parse_args()
    try:
        arguments=json.loads(a.arguments)
        if not isinstance(arguments,dict):raise ValueError()
    except ValueError:p.error('--arguments must be a JSON object')
    result=call(a.operation,arguments,{'endpoint':a.endpoint,'token_file':a.token_file})
    print(json.dumps(result,indent=2,ensure_ascii=False));raise SystemExit(0 if result.get('ok') else 1)
if __name__=='__main__':main()
