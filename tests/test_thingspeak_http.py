import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from functools import partial

import pytest

from edge_api.infrastructure.thingspeak_client import ThingSpeakClient,http_exchange,_retry_after


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        self.server.calls.append({'path':self.path,'headers':dict(self.headers),
            'body':self.rfile.read(int(self.headers.get('Content-Length','0'))).decode()})
        self.send_response(self.server.status)
        if self.server.retry is not None:self.send_header('Retry-After',self.server.retry)
        self.send_header('Location',self.server.url+'/secret-sink')
        self.send_header('Content-Length',str(len(self.server.body)));self.end_headers()
        if self.server.slow:time.sleep(self.server.slow)
        try:self.wfile.write(self.server.body)
        except (BrokenPipeError,ConnectionResetError):pass


@pytest.fixture
def http_double():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.url=f'http://127.0.0.1:{server.server_port}'
    server.status=200;server.body=b'123';server.retry=None;server.slow=0;server.calls=[]
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join(3)


@pytest.mark.parametrize('status,body,state,code',[
    (200,b'123','CONFIRMED','ENTRY_ACCEPTED'),(200,b'0','PENDING','ZERO_ENTRY'),
    (200,b'','UNCERTAIN','INVALID_BODY'),(200,b'12 trailing','UNCERTAIN','INVALID_BODY'),
    (200,b'"123"','UNCERTAIN','INVALID_BODY'),(200,b'9'*90,'UNCERTAIN','INVALID_BODY'),
    (403,b'echo-credential','REJECTED','HTTP_403'),(500,b'12','UNCERTAIN','HTTP_500'),
    (302,b'','REJECTED','REDIRECT_REFUSED')])
def test_full_http_contract_and_header_only_key(http_double,status,body,state,code,caplog):
    server=http_double;server.status=status;server.body=body
    result=http_exchange('synthetic-private-key',{'field1':'5.0','status':'mg:local-id'},1,server.url+'/update')
    assert result.state==state and result.code==code
    assert len(server.calls)==1 and server.calls[0]['path']=='/update'
    assert server.calls[0]['headers']['THINGSPEAKAPIKEY']=='synthetic-private-key'
    assert 'synthetic-private-key' not in server.calls[0]['body'] and 'synthetic-private-key' not in caplog.text
    if state=='CONFIRMED':assert result.entry_id==123


def test_tls_verify_redirect_and_ambient_credentials_policy(monkeypatch):
    import requests
    observed={}
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_content(self,**k):yield b'1'
    class Session:
        trust_env=True
        def post(self,url,**kwargs):observed.update(url=url,trust=self.trust_env,**kwargs);return Response()
        def close(self):pass
    monkeypatch.setattr(requests,'Session',Session)
    assert http_exchange('synthetic-private-key',{'field1':'5'},1).state=='CONFIRMED'
    assert observed['url']=='https://api.thingspeak.com/update'
    assert observed['verify'] is True and observed['allow_redirects'] is False and observed['trust'] is False


def test_retry_after_seconds_date_and_invalid(http_double):
    from datetime import datetime,timedelta,timezone
    from email.utils import format_datetime
    http_double.status=429;http_double.retry='45'
    assert http_exchange('synthetic-private-key',{'field1':'5'},1,http_double.url).retry_after==45
    delay=_retry_after(format_datetime(datetime.now(timezone.utc)+timedelta(seconds=90)))
    assert 88<delay<=90
    assert _retry_after('bad')==0 and _retry_after('999999999') is None


def test_process_bounds_lost_response_and_cancellation(http_double):
    http_double.slow=2
    client=ThingSpeakClient(.8,partial(http_exchange,url=http_double.url+'/update'))
    start=time.monotonic();result=client.publish('synthetic-private-key',{'field1':'5'})
    assert result.state=='UNCERTAIN' and time.monotonic()-start<3
    # Parent stops the HTTP child even while it waits for a committed remote response.
    event=threading.Event();threading.Timer(.15,event.set).start()
    start=time.monotonic();result=client.publish('synthetic-private-key',{'field1':'5'},event.is_set)
    assert result.code=='CANCELLED' and time.monotonic()-start<2


def immediate_result(*args):
    from edge_api.infrastructure.thingspeak_client import ThingSpeakResult
    return ThingSpeakResult('CONFIRMED','ENTRY_ACCEPTED',1)


def test_http_child_exits_cleanly_when_owner_pipe_has_closed():
    import multiprocessing
    from edge_api.infrastructure.thingspeak_client import _child
    context=multiprocessing.get_context('spawn');parent,child=context.Pipe(duplex=False)
    process=context.Process(target=_child,args=(child,immediate_result,'synthetic-private-key',{'field1':'5'},1))
    parent.close();process.start();child.close();process.join(3)
    try:assert process.exitcode==0
    finally:
        if process.is_alive():process.terminate();process.join(2)
        process.close()
