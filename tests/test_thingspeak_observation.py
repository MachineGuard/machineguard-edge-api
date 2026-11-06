"""Loopback-only observability contracts; diagnostic progress is never a receipt."""
import json
import logging
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import pytest
from conftest import post_reading
from test_thingspeak import secondary
from edge_api.infrastructure.models import database,LocalBufferEntryModel

from edge_api.infrastructure.thingspeak_client import ThingSpeakClient,http_exchange
from edge_api.infrastructure import thingspeak_client as client_module


def observed(client):
    value=getattr(client,'last_observation',None)
    assert value is not None, 'Last bounded phase observation is missing'
    return value


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.server.entered.set();time.sleep(self.server.before_headers)
        try:
            self.send_response(200);self.send_header('Content-Length',str(len(self.server.body)));self.end_headers()
            if self.server.during_body:
                self.wfile.write(self.server.body[:1]);self.wfile.flush();time.sleep(self.server.during_body)
                self.wfile.write(self.server.body[1:])
            else:self.wfile.write(self.server.body)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass


@pytest.fixture
def observed_server():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.before_headers=0;server.during_body=0;server.body=b'123';server.entered=threading.Event()
    server.url=f'http://127.0.0.1:{server.server_port}/update'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join(3)


@pytest.mark.parametrize('delay,last_http', [('before_headers','HTTP_STARTED'),('during_body','HEADERS_RECEIVED')])
def test_uncertain_keeps_last_observed_http_phase(observed_server,delay,last_http,caplog):
    setattr(observed_server,delay,5)
    client=ThingSpeakClient(3,partial(http_exchange,url=observed_server.url))
    started=time.monotonic();result=client.publish('synthetic-private-key',{'field1':'5'})
    assert result.state=='UNCERTAIN' and time.monotonic()-started<4.5
    observation=getattr(client,'last_observation',None)
    assert observation is not None, 'No bounded phase observation retained on uncertain result'
    assert observation['lastHttpPhase']==last_http
    assert observation['resultCode']==result.code
    assert observation['phaseAttribution']=='OBSERVED_ONLY'
    assert len(observation['events'])<=12
    assert 'synthetic-private-key' not in json.dumps(observation)+caplog.text


def test_valid_receipt_reports_phase_sequence_without_payload(observed_server,caplog):
    client=ThingSpeakClient(5,partial(http_exchange,url=observed_server.url))
    secret='synthetic-private-key'
    result=client.publish(secret,{'field1':'5','status':'sensitive-body-marker'})
    assert result.state=='CONFIRMED' and result.entry_id==123
    observation=getattr(client,'last_observation',None)
    assert observation is not None, 'Valid receipt lacks phase observation'
    phases=[event['phase'] for event in observation['events']]
    assert phases==['START_REQUESTED','START_RETURNED','CHILD_ENTERED','HTTP_STARTED',
        'HEADERS_RECEIVED','BODY_COMPLETED','RESPONSE_VALIDATED','RESULT_READY','RESULT_RECEIVED','FINAL_RESULT']
    assert len(observation['attemptId'])==32
    assert observation['lastHttpPhase']=='RESPONSE_VALIDATED'
    assert secret not in json.dumps(observation)+caplog.text and 'sensitive-body-marker' not in json.dumps(observation)+caplog.text


class WireConnection:
    """IPC fault double: frames or an indefinitely incomplete write until the owner closes."""
    def __init__(self,frames):
        self.frames=frames;self.index=0;self.closed=threading.Event();self.started=0;self.attempt='0'*32
    def close(self):self.closed.set()
    def poll(self,seconds):
        time.sleep(min(seconds,.005));return self.index<len(self.frames)
    def _next(self):
        if self.index>=len(self.frames):self.closed.wait(2);raise EOFError()
        delay,value=self.frames[self.index]
        if self.closed.wait(max(0,self.started+delay-time.monotonic())):raise EOFError()
        self.index+=1
        if isinstance(value,bytes):return value
        return json.dumps({'v':1,'attemptId':self.attempt,**value}).encode()
    def recv_bytes(self,maximum):
        value=self._next()
        if len(value)>maximum:raise OSError('oversized diagnostic packet')
        return value
    def recv(self):return json.loads(self._next())  # Base client's pre-protocol behavior.


class WireContext:
    def __init__(self,frames):self.parent=WireConnection(frames)
    def Pipe(self,**kwargs):return self.parent,SimplePipe()
    def Process(self,target,args,daemon):
        parent=self.parent
        if len(args)==6:parent.attempt=args[-1]
        class Process:
            pid=1234;alive=True
            def start(self):parent.started=time.monotonic()
            def is_alive(self):return self.alive
            def terminate(self):self.alive=False;parent.close()
            def kill(self):self.terminate()
            def join(self,**kwargs):pass
            def close(self):pass
        return Process()


class SimplePipe:
    def close(self):pass


def phase(name,elapsed=1):return {'kind':'progress','phase':name,'elapsedMs':elapsed}


def wire_client(monkeypatch,frames,seconds=.3):
    context=WireContext(frames)
    monkeypatch.setattr(client_module.multiprocessing,'get_context',lambda name:context)
    return ThingSpeakClient(seconds)


def test_progress_without_receipt_does_not_extend_deadline(monkeypatch):
    frames=[(i*.04,phase(name,i*40)) for i,name in enumerate([
        'CHILD_ENTERED','HTTP_STARTED','HEADERS_RECEIVED','BODY_COMPLETED','RESPONSE_VALIDATED','RESULT_READY'])]
    client=wire_client(monkeypatch,frames)
    started=time.monotonic();result=client.publish('',{})
    assert isinstance(result,client_module.ThingSpeakResult) and result.state=='UNCERTAIN' and result.code=='TIMEOUT'
    assert .25<time.monotonic()-started<1
    assert observed(client)['lastChildPhase']=='RESULT_READY'
    assert observed(client)['readerStopped'] is True


def test_incomplete_ipc_write_cannot_block_deadline_or_invent_child_phase(monkeypatch):
    client=wire_client(monkeypatch,[])
    started=time.monotonic();result=client.publish('',{})
    assert result.code=='TIMEOUT' and time.monotonic()-started<1
    assert observed(client)['lastChildPhase']=='UNKNOWN' and observed(client)['lastHttpPhase']=='UNKNOWN'
    assert observed(client)['readerStopped']


@pytest.mark.parametrize('raw',[b'{"v":1,"kind":"result"',b'x'*513,
    b'{"v":1,"kind":"result","attemptId":"wrong","result":{"state":"CONFIRMED"}}'])
def test_malformed_or_oversized_messages_never_confirm(monkeypatch,raw,caplog):
    client=wire_client(monkeypatch,[(0,phase('CHILD_ENTERED')),(.01,raw)])
    try:result=client.publish('synthetic-private-key',{})
    except Exception:pytest.fail('Malformed IPC escaped instead of preserving uncertainty')
    assert isinstance(result,client_module.ThingSpeakResult) and result.state=='UNCERTAIN'
    assert observed(client)['lastChildPhase']=='CHILD_ENTERED'
    assert observed(client)['lastHttpPhase']=='UNKNOWN'
    assert len(observed(client)['events'])<=12 and len(caplog.text)<4096


def test_progress_flood_is_bounded_and_cannot_confirm(monkeypatch,caplog):
    frames=[(0,phase('CHILD_ENTERED'))]+[(0,phase('HTTP_STARTED',i)) for i in range(100)]
    client=wire_client(monkeypatch,frames)
    result=client.publish('',{})
    assert isinstance(result,client_module.ThingSpeakResult) and result.state=='UNCERTAIN' and result.code=='INVALID_PROTOCOL'
    assert observed(client)['diagnosticsLimited'] and len(observed(client)['events'])<=12
    assert len(caplog.text)<4096


def test_valid_final_receipt_can_arrive_without_progress(monkeypatch):
    frame={'kind':'result','result':{'state':'CONFIRMED','code':'ENTRY_ACCEPTED','entryId':42,'retryAfter':0,'httpStatus':200}}
    client=wire_client(monkeypatch,[(0,frame)])
    result=client.publish('',{})
    assert isinstance(result,client_module.ThingSpeakResult) and result.state=='CONFIRMED' and result.entry_id==42
    assert observed(client)['lastChildPhase']=='UNKNOWN' and observed(client)['lastHttpPhase']=='UNKNOWN'


def exit_without_receipt(*args):
    import os
    os._exit(4)


def test_lost_child_keeps_its_observed_entry():
    client=ThingSpeakClient(5,exit_without_receipt)
    result=client.publish('',{})
    assert result.state=='UNCERTAIN' and result.code=='TRANSPORT_EXITED'
    assert observed(client)['lastChildPhase']=='CHILD_ENTERED'
    assert observed(client)['lastHttpPhase']=='UNKNOWN' and observed(client)['readerStopped']


def test_cancel_preserves_observed_phase_and_closes_reader(monkeypatch):
    client=wire_client(monkeypatch,[(0,phase('CHILD_ENTERED')),(.01,phase('HTTP_STARTED'))],seconds=1)
    cancelled=threading.Event();timer=threading.Timer(.1,cancelled.set);timer.start()
    try:result=client.publish('',{},cancelled.is_set)
    finally:timer.cancel()
    assert isinstance(result,client_module.ThingSpeakResult) and result.code=='CANCELLED' and observed(client)['lastHttpPhase']=='HTTP_STARTED'
    assert observed(client)['readerStopped']


def test_response_and_exception_secrets_are_absent_from_diagnostic(observed_server,caplog):
    caplog.set_level(logging.INFO)
    observed_server.body=b'synthetic-body-marker synthetic-private-key'
    client=ThingSpeakClient(5,partial(http_exchange,url=observed_server.url))
    result=client.publish('synthetic-private-key',{'field1':'synthetic-payload-marker'})
    assert result.code=='INVALID_BODY'
    assert 'ThingSpeak diagnostic' in caplog.text and 'BODY_COMPLETED' in caplog.text
    for secret in ('synthetic-private-key','synthetic-body-marker','synthetic-payload-marker'):
        assert secret not in caplog.text+json.dumps(observed(client))
    assert len([record for record in caplog.records if record.name==client_module.__name__])==1
    assert len(caplog.text)<4096


def test_logging_failure_cannot_replace_valid_receipt(monkeypatch):
    frame={'kind':'result','result':{'state':'CONFIRMED','code':'ENTRY_ACCEPTED','entryId':42,'retryAfter':0,'httpStatus':200}}
    client=wire_client(monkeypatch,[(0,frame)])
    from types import SimpleNamespace
    monkeypatch.setattr(client_module,'logger',SimpleNamespace(log=lambda *a,**k:(_ for _ in ()).throw(OSError('synthetic logging failure'))),raising=False)
    result=client.publish('',{})
    assert isinstance(result,client_module.ThingSpeakResult) and result.state=='CONFIRMED'


def test_zero_body_is_validated_but_never_confirmed(observed_server):
    observed_server.body=b'0'
    client=ThingSpeakClient(5,partial(http_exchange,url=observed_server.url))
    result=client.publish('synthetic-private-key',{})
    assert result.state=='PENDING' and result.code=='ZERO_ENTRY' and result.entry_id is None
    assert observed(client)['lastHttpPhase']=='RESPONSE_VALIDATED'


def test_instrumented_io_keeps_capture_primary_and_health_independent(secondary,clock,cloud,observed_server):
    client,services=secondary;observed_server.before_headers=5
    services.thingspeak.client=ThingSpeakClient(3,partial(http_exchange,url=observed_server.url))
    post_reading(client,25,50,device='selected');services.thingspeak._plan(clock());clock.advance(seconds=20)
    errors=[]
    def cycle():
        try:services.thingspeak.cycle()
        except BaseException as error:errors.append(type(error).__name__)
        finally:
            if not database.is_closed():database.close()
    thread=threading.Thread(target=cycle);thread.start()
    try:
        assert observed_server.entered.wait(3)
        started=time.monotonic();assert post_reading(client,device='selected').status_code==201
        assert time.monotonic()-started<1 and len(cloud.published)==2 and LocalBufferEntryModel.select().count()==0
        thread.join(5);assert not thread.is_alive() and not errors
        health=client.get('/api/v1/edge/health').json['thingspeak']
        assert health['uncertain']==1
        diagnostic=health.get('lastObservation')
        assert diagnostic is not None, 'Health lacks correlation for the completed instrumented attempt'
        assert diagnostic['lastHttpPhase']=='HTTP_STARTED'
        assert diagnostic['jobId']==client.get('/api/v1/edge/thingspeak/jobs').json['items'][0]['id']
        assert 'synthetic-private-key' not in json.dumps(health) and 'synthetic-secondary-only' not in json.dumps(health)
        assert client.get('/api/v1/edge/health',headers={'X-Node-Credential':client.tokens['selected']}).status_code==403
    finally:
        thread.join(5);services.runtime.stop()


@pytest.mark.parametrize('entry,status',[(None,200),(0,200),(True,200),(42,403)])
def test_invalid_final_receipt_cannot_confirm_even_with_progress(monkeypatch,entry,status):
    frame={'kind':'result','result':{'state':'CONFIRMED','code':'ENTRY_ACCEPTED','entryId':entry,'retryAfter':0,'httpStatus':status}}
    client=wire_client(monkeypatch,[(0,phase('CHILD_ENTERED')),(.01,frame)])
    result=client.publish('',{})
    assert isinstance(result,client_module.ThingSpeakResult) and result.state=='UNCERTAIN'
