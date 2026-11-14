"""HTTP clients with fixed server-owned endpoints and no redirect credential leaks."""
import httpx
from urllib.parse import quote
from .contracts import Fault

def unconfigured(adapter, code):
    """Retain adapter shape while refusing I/O before any socket is opened."""
    def refuse(request):
        raise Fault(code, 'configure this backend explicitly')
    client=adapter('http://unconfigured.invalid')
    client.http.close()
    client.http=httpx.Client(base_url=client.base,transport=httpx.MockTransport(refuse),trust_env=False)
    return client

class JsonHTTP:
    def __init__(self,base,token='',timeout=15):
        self.base=base.rstrip('/');self.http=httpx.Client(base_url=self.base,headers={'Authorization':'Bearer '+token} if token else {},timeout=timeout,follow_redirects=False,trust_env=False)
    def call(self,method,path,body=None):
        try:
            r=self.http.request(method,path,json=body)
            if r.status_code>=300:raise Fault('REMOTE_HTTP_'+str(r.status_code),'upstream request rejected')
            return r.json() if r.content else None
        except (httpx.HTTPError,ValueError) as e:raise Fault('UPSTREAM_UNAVAILABLE',type(e).__name__) from e
class Invoke(JsonHTTP):
    queue='default'
    @property
    def qp(self):return '/api/v1/queue/'+quote(self.queue,safe='')
    def models(self):
        p=self.call('GET','/api/v2/models/');return p.get('models',p.get('items',[]))
    def item(self,nid):return self.call('GET',self.qp+'/i/'+str(int(nid)))
    def inventory(self):
        ids=self.call('GET',self.qp+'/item_ids?order_dir=ASC')
        if len(ids['item_ids'])!=ids['total_count']:raise Fault('QUEUE_INCOMPLETE','ID inventory count mismatch')
        out=[]
        for start in range(0,len(ids['item_ids']),50):
            out.extend(self.call('POST',self.qp+'/items_by_ids',{'item_ids':ids['item_ids'][start:start+50]}))
        observed=[item.get('item_id') for item in out]
        if len(set(observed))!=len(observed) or set(observed)!=set(ids['item_ids']):
            raise Fault('QUEUE_INCOMPLETE','native inventory changed during hydration; retry next scan, never infer missing jobs as canceled')
        return out
    def inventory_get(self,stop=None):
        """GET-only hydration for configured-source adoption observation."""
        ids=self.call('GET',self.qp+'/item_ids?order_dir=ASC')
        values=ids.get('item_ids',[])
        if len(values)!=ids.get('total_count') or len(set(values))!=len(values):
            raise Fault('QUEUE_INCOMPLETE','native ID inventory incomplete')
        out=[]
        for n in values:
            if stop is not None and stop.is_set():raise Fault('OBSERVER_STOPPING','observer stopping during GET hydration')
            out.append(self.item(n))
        if [i.get('item_id') for i in out]!=values:
            raise Fault('QUEUE_INCOMPLETE','native item identity changed')
        return out
    def submit(self,graph,jid):
        return self.call('POST',self.qp+'/enqueue_batch',{'batch':{'batch_id':jid,'graph':graph,'runs':1,'origin':'atlas-engine:'+jid},'prepend':False})
    def submit_prepared(self,body):
        return self.call('POST',self.qp+'/enqueue_batch',body)
    def enqueue_receipt(self,key):
        return self.call('GET',self.qp+'/enqueue_batch/receipt?idempotency_key='+quote(key,safe=''))
    def acknowledge_enqueue(self,key):
        return self.call('POST',self.qp+'/enqueue_batch/acknowledge',{'idempotency_key':key})
    def cancel(self,nid):return self.call('PUT',self.qp+'/i/'+str(int(nid))+'/cancel')
    def upload(self,raw,filename,*,board_id=None,is_intermediate=True):
        params={'image_category':'user','is_intermediate':str(is_intermediate).lower()}
        if board_id is not None:params['board_id']=board_id
        r=self.http.post('/api/v1/images/upload',params=params,files={'file':(filename,raw,'image/png' if filename.endswith('.png') else 'image/jpeg')})
        if r.status_code>=300:raise Fault('UPLOAD_HTTP','native upload rejected')
        return r.json()
class Atlas(JsonHTTP):
    def __init__(self,base,collection='prompt_atlas',token=''):
        super().__init__(base,token);self.collection=collection
    def scroll(self,conditions,limit=64,offset=None,vectors=False):
        body={'filter':{'must':conditions},'limit':limit,'with_payload':True,'with_vector':vectors}
        if offset is not None:body['offset']=offset
        return self.call('POST','/collections/'+quote(self.collection,safe='')+'/points/scroll',body)['result']
    @staticmethod
    def eq(k,v):return {'key':k,'match':{'value':v}}
    def evidence(self,kind,key):
        field='tag' if kind=='tag' else 'asset_id';typ='danbooru_tag' if kind=='tag' else 'model_asset'
        return self.scroll([self.eq('kind',typ),self.eq(field,key)],10)['points']
    def query(self,vector,limit):
        return self.call('POST','/collections/'+quote(self.collection,safe='')+'/points/query',{'query':vector,'using':'semantic','limit':limit,'with_payload':True,'filter':{'should':[self.eq('kind','danbooru_tag'),self.eq('kind','model_asset')]}})['result']['points']
    def resources(self):
        out=[];off=None
        while True:
            r=self.scroll([self.eq('kind','model_asset')],128,off);out+=r['points'];off=r.get('next_page_offset')
            if off is None:return out
