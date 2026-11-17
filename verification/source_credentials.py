"""Owned HTTP source: explicit metadata bearer, never image/redirect leakage."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import socket
from unittest.mock import patch
from atlas_engine.onboarding import Onboarding
from atlas_engine.contracts import Fault


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False,mode=0o700)
    token=secrets.token_urlsafe(40);observed=[]
    class Source(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            observed.append({'path':self.path,'authorization_present':bool(self.headers.get('Authorization')),'cookie_present':bool(self.headers.get('Cookie'))})
            if self.path in ('/metadata','/echo'):
                assert self.headers.get('Authorization')=='Bearer '+token
                raw=json.dumps({'ok':True,**({'echo':token} if self.path=='/echo' else {})}).encode()
            else:
                assert not self.headers.get('Authorization') and not self.headers.get('Cookie')
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','/image');self.end_headers();return
                raw=b'image representation'
            self.send_response(200);self.send_header('Content-Length',str(len(raw)))
            self.send_header('Set-Cookie','must_not_leak=yes');self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Source);t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
    base='http://127.0.0.1:'+str(server.server_address[1]);o=Onboarding(None,base,token)
    try:
        assert o.json(base+'/metadata')=={'ok':True}
        assert o.request(base+'/redirect',image=True)[0]==b'image representation'
        try:o.json(base+'/echo')
        except Fault as e:assert e.code=='SOURCE_REDACTED'
        else:raise AssertionError('credential echo accepted')
        # URL validation checks DNS before the source allowlist. Make only that
        # lookup deterministic: no public request is authorized by this fixture.
        before = len(observed)
        for address in ('93.184.216.34', '127.0.0.1'):
            with patch('atlas_engine.onboarding.socket.getaddrinfo', return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]) as dns:
                try:o.json('https://civitai.com/api/v1/models/1')
                except Fault as e:assert e.code=='SOURCE_URL'
                else:raise AssertionError('unconfigured source accepted')
                dns.assert_called_once()
        assert len(observed) == before
        result={'status':'pass','checks':['explicit metadata credential','fresh per-hop image clients without auth/cookie','credential echo refused','wrong source refused with deterministic public DNS; private DNS still refused'],'requests':observed}
        (a.out/'result.json').write_text(json.dumps(result));print(json.dumps(result))
    finally:server.shutdown();server.server_close();t.join()


if __name__=='__main__':main()
