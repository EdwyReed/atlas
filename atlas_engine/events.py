"""Read-only native event subscription. Hints trigger authoritative queue hydration."""
import threading,time
import requests,socketio
EVENTS=('batch_enqueued','queue_item_status_changed','queue_items_canceled','queue_items_retried','queue_cleared','invocation_complete','invocation_error')
class NativeEvents:
    def __init__(self,invoke,repo,dirty,wake,scope=None):
        self.invoke=invoke;self.repo=repo;self.dirty=dirty;self.wake=wake;self.stop=threading.Event();self.sio=None;self.thread=None
        self.scope=scope
    def health(self,key,value=None):
        if self.scope:key=key+':'+self.scope['connection_id']
        return self.repo.health(key,value) if value is not None else self.repo.health(key)
    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True,name='invoke-event-observer');self.thread.start()
    def run(self):
        session=requests.Session();session.trust_env=False
        self.sio=socketio.Client(reconnection=True,request_timeout=5,http_session=session,logger=False,engineio_logger=False)
        @self.sio.event
        def connect():
            self.sio.emit('subscribe_queue',{'queue_id':self.invoke.queue});self.dirty.set();self.wake.set()
            self.health('native_events',{'connected':True,'at':time.time(),'socket_path':'ws/socket.io','transport':'socketio-polling','subscription':'subscribe_queue','events_seen':self.health('native_event_count') or 0})
        @self.sio.event
        def disconnect(*args):self.health('native_events',{'connected':False,'at':time.time()})
        for name in EVENTS:self.sio.on(name,lambda data,n=name:self.receive(n,data))
        while not self.stop.is_set():
            try:
                headers=dict(self.invoke.http.headers)
                self.sio.connect(self.invoke.base,headers=headers,socketio_path='ws/socket.io',transports=['polling'],wait_timeout=5)
                while self.sio.connected and not self.stop.wait(.5):pass
                if self.sio.connected:self.sio.eio.disconnect(abort=True)
            except Exception as e:self.health('native_events',{'connected':False,'at':time.time(),'error':type(e).__name__})
            self.stop.wait(10)
    def receive(self,name,data):
        if not isinstance(data,dict):return
        if data.get('queue_id',self.invoke.queue)!=self.invoke.queue:return
        # No preview image, raw prompt, graph or arbitrary event payload is journaled.
        safe={k:data[k] for k in ('queue_id','item_id','batch_id','session_id','status','status_sequence','timestamp') if k in data}
        if self.scope:safe['native_scope']=self.scope
        self.repo.event('native-events',name,safe)
        n=(self.health('native_event_count') or 0)+1;self.health('native_event_count',n)
        self.health('native_events',{'connected':True,'at':time.time(),'last_event':name,'events_seen':n})
        self.dirty.set();self.wake.set()
    def close(self):
        self.stop.set()
        # Engine.IO's ordinary disconnect joins a blocking long-poll reader.
        # Two observers must not serialize two server ping timeouts on shutdown.
        if self.sio and self.sio.connected:self.sio.eio.disconnect(abort=True)
        if self.thread:self.thread.join(timeout=8)
