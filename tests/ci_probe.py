"""Socket contract used by Core's portable integration runner. Synthetic/local services only."""
import json,os,secrets,socket,subprocess,sys,threading,time,uuid
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from functools import partial
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import requests
from edge_api import create_app
from edge_api.config import Config
from edge_api.infrastructure.models import database
from edge_api.infrastructure.thingspeak_client import ThingSpeakClient,http_exchange


def wait_for(predicate,seconds=8):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.05)
    raise AssertionError('Contract condition timed out')


class Clock:
    def __init__(self):
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        self.now=now-timedelta(seconds=now.second%20,microseconds=now.microsecond)
    def __call__(self):return self.now
    def advance(self,seconds):self.now+=timedelta(seconds=seconds)
    def mono(self):return (self.now-datetime(1970,1,1)).total_seconds()


class DoubleHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0'))).decode()
        assert 'THINGSPEAKAPIKEY' in self.headers
        assert self.headers['THINGSPEAKAPIKEY'] not in self.path+body
        self.server.calls.append({'path':self.path,'body':body,'at':time.monotonic()})
        if self.server.status==200 and self.server.body not in (b'0',b''):
            self.server.remote_entries+=1
        self.server.entered.set()
        if self.server.drop:
            self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
        self.send_response(self.server.status)
        if self.server.status==429:self.send_header('Retry-After','45')
        self.send_header('Content-Length',str(len(self.server.body)));self.end_headers()
        time.sleep(self.server.slow)
        try:self.wfile.write(self.server.body)
        except (BrokenPipeError,ConnectionResetError):pass


@contextmanager
def secondary_double(status=200,body=b'91',slow=0,drop=False):
    server=ThreadingHTTPServer(('127.0.0.1',0),DoubleHandler)
    server.status,server.body,server.slow,server.drop=status,body,slow,drop
    server.calls=[];server.remote_entries=0;server.entered=threading.Event()
    server.url=f'http://127.0.0.1:{server.server_port}/update'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join(3)


@contextmanager
def serve(app):
    from waitress import create_server,wasyncore
    server=create_server(app,host='127.0.0.1',port=0,threads=4,asyncore_loop_timeout=.05)
    stopped=threading.Event();errors=[]
    def loop():
        try:
            while not stopped.is_set():wasyncore.loop(timeout=.05,map=server._map,count=1)
        except Exception as error:errors.append(type(error).__name__)
        finally:
            server.task_dispatcher.shutdown(timeout=8);wasyncore.close_all(server._map)
    thread=threading.Thread(target=loop);thread.start()
    try:yield f'http://127.0.0.1:{server.effective_port}/api/v1/edge'
    finally:
        app.config['services'].runtime.stop();stopped.set();thread.join(10)
        assert not thread.is_alive() and not errors


class Case:
    def __init__(self,run,core,name):
        self.run,self.core,self.name=run,core,name;self.clock=Clock();self.headers={'X-Edge-Admin-Key':run.edge_key}
        suffix=uuid.uuid4().hex[:10];self.code='f7-'+suffix
        self.zone=core('POST','/api/v1/environmental-monitoring/zones',{'name':'F7 '+suffix},201)['id']
        point=core('POST',f'/api/v1/environmental-monitoring/zones/{self.zone}/points',{'name':'F7 point'},201)['id']
        self.sensor=core('POST',f'/api/v1/environmental-monitoring/zones/{self.zone}/points/{point}/sensors',
            {'deviceCode':self.code,'samplingIntervalSeconds':60},201)['id']
        for variable,low,high in [('TEMPERATURE',2,8),('HUMIDITY',30,75)]:
            core('PUT',f'/api/v1/environmental-monitoring/zones/{self.zone}/thresholds/{variable}',{'minimumValue':low,'maximumValue':high})
        self.gateway=core('POST','/api/v1/gateways',{'name':'F7 synthetic','sensorNodeIds':[self.sensor]},201)
        self.config=Config(database_path=str(run.runtime/(name+'.db')),gateway_id=self.gateway['gateway']['id'],
            cloud_base_url=run.base,device_credential=self.gateway['credential'],admin_key=run.edge_key,
            enable_availability_loop=False,delivery_poll_seconds=.05,delivery_backoff_seconds=.1,
            delivery_max_backoff_seconds=1,delivery_http_timeout_seconds=2,delivery_lease_seconds=6,
            thingspeak_enabled=True,thingspeak_device_code=self.code,thingspeak_channel_id='123',
            thingspeak_write_key='synthetic-secondary-ci-only',thingspeak_poll_seconds=60,thingspeak_http_seconds=5)
        self.app=create_app(self.config,clock=self.clock);self.services=self.app.config['services']
        self.services.thingspeak.monotonic=self.clock.mono
        self.server=serve(self.app);self.base=self.server.__enter__()
        response=self.edge('POST','/sensor-nodes',{'deviceCode':self.code,'samplingIntervalSeconds':10},201)
        self.node={'X-Node-Credential':response['nodeCredential']}

    def edge(self,method,path,body=None,expected=200,headers=None):
        response=requests.request(method,self.base+path,json=body,headers=headers or self.headers,timeout=8)
        assert response.status_code==expected, f'Edge {method} {path}: {response.status_code}'
        return response.json()

    def send(self,seq):
        return self.edge('POST','/readings',{'deviceCode':self.code,'sampleId':f'f7-{seq}',
            'temperature':25,'humidity':50},201,self.node)

    def history(self):
        return self.core('GET','/api/v1/environmental-monitoring/measurements/history',params={
            'monitoringZoneId':self.zone,'from':(self.clock()-timedelta(days=1)).isoformat()+'Z',
            'to':(self.clock()+timedelta(days=1)).isoformat()+'Z'})['totalElements']

    def verify_core(self,count):
        wait_for(lambda:self.history()==count)
        assert len(self.core('GET','/api/v1/alerts',params={'monitoringZoneId':self.zone}))==1
        latest=self.core('GET','/api/v1/environmental-monitoring/measurements/latest',params={'monitoringZoneId':self.zone})
        assert all(row['source']=='SENSOR' and row['freshness']=='FRESH' for row in latest)

    def close(self):
        self.server.__exit__(None,None,None)
        self.core('POST',f"/api/v1/gateways/{self.gateway['gateway']['id']}/revoke",expected=200)


def execute(run):
    login=requests.post(run.base+'/api/v1/auth/login',json={'email':'admin@ci.machineguard.invalid','password':run.admin_password},timeout=10)
    assert login.status_code==200
    bearer={'Authorization':'Bearer '+login.json()['accessToken']}
    def core(method,path,body=None,expected=200,params=None):
        response=requests.request(method,run.base+path,json=body,headers=bearer,params=params,timeout=15)
        assert response.status_code==expected,f'Core {method} {path}: {response.status_code}'
        return response.json() if response.content else None
    core('POST','/api/v1/auth/validate')  # Real persisted session, user and organization.
    result={'transport':'Waitress HTTP -> Core/PostgreSQL real; ThingSpeak loopback double',
        'clock':'Controlled receive/window/monotonic time; HTTP and process timings are wall time','cases':[]}
    for name,status,body,slow,drop,expected in [
        ('zero',200,b'0',0,False,'PENDING'),('unavailable',503,b'',0,False,'UNCERTAIN'),
        ('slow',200,b'91',7,False,'UNCERTAIN'),('lost-response',200,b'91',0,True,'UNCERTAIN'),
        ('forbidden',403,b'key-rejected',0,False,'REJECTED'),('rate-limit',429,b'',0,False,'PENDING'),
        ('invalid-body',200,b'bad',0,False,'UNCERTAIN'),('empty-body',200,b'',0,False,'UNCERTAIN'),
        ('network-down',200,b'91',0,False,'UNCERTAIN')]:
        print('Secondary socket case:',name,flush=True)
        case=Case(run,core,name)
        try:
            with secondary_double(status,body,slow,drop) as double:
                url=double.url
                if name=='network-down':
                    with socket.socket() as closed:closed.bind(('127.0.0.1',0));port=closed.getsockname()[1]
                    url=f'http://127.0.0.1:{port}/update'
                case.services.thingspeak.client=ThingSpeakClient(5,partial(http_exchange,url=url))
                case.send(0);case.services.thingspeak.cycle();case.clock.advance(10)
                case.send(1);case.services.thingspeak.cycle();case.clock.advance(10)
                errors=[]
                def cycle():
                    try:case.services.thingspeak.cycle()
                    except BaseException as error:errors.append(type(error).__name__)
                    finally:
                        if not database.is_closed():database.close()
                thread=threading.Thread(target=cycle);thread.start()
                if name!='network-down':
                    try:wait_for(double.entered.is_set)
                    except AssertionError:
                        run.result['secondaryFailure']={'case':name,'health':case.edge('GET','/health')['thingspeak'],
                            'jobs':case.edge('GET','/thingspeak/jobs'),'threadErrors':errors}
                        run.save();raise
                started=time.monotonic();case.send(2);capture_ms=round((time.monotonic()-started)*1000,3)
                thread.join(5);assert not thread.is_alive() and not errors
                assert capture_ms<1500
                page=case.edge('GET','/thingspeak/jobs')['items']
                allowed={'PENDING','UNCERTAIN'} if name=='network-down' else {expected}
                assert page[0]['state'] in allowed,(name,page[0]['state'])
                actual=page[0]['state']
                case.verify_core(6);case.send(0);assert case.history()==6
                calls=len(double.calls);case.services.thingspeak.cycle();assert len(double.calls)==calls
                assert 'synthetic-secondary-ci-only' not in json.dumps(case.edge('GET','/health'))
                result['cases'].append({'case':name,'coreMeasurements':6,'alerts':1,'secondaryState':actual,
                    'remoteDoubleEntries':double.remote_entries,'captureWallMs':capture_ms,'primaryPending':case.edge('GET','/buffer')['pendingCount']})
        finally:case.close()
    case=Case(run,core,'core-outage')
    try:
        with secondary_double() as double:
            case.services.thingspeak.client=ThingSpeakClient(5,partial(http_exchange,url=double.url))
            run.stop_core();outage=time.monotonic()
            case.send(0);case.services.thingspeak.cycle();case.clock.advance(10)
            case.send(1);case.services.thingspeak.cycle();case.clock.advance(10);case.services.thingspeak.cycle()
            assert case.edge('GET','/thingspeak/jobs')['items'][0]['state']=='CONFIRMED'
            assert case.edge('GET','/buffer')['pendingCount']==2
            # Both downstreams unavailable, then actual Core restart and secondary recovery.
            double.status=200;double.body=b'0'
            case.send(2);case.services.thingspeak.cycle();case.clock.advance(20);case.services.thingspeak.cycle()
            assert case.edge('GET','/buffer')['pendingCount']==3
            run.start_core();case.clock.advance(1)
            wait_for(lambda:case.edge('GET','/buffer')['pendingCount']==0)
            case.verify_core(6)
            double.body=b'91';case.clock.advance(20);case.services.thingspeak.cycle()
            assert all(row['state']=='CONFIRMED' for row in case.edge('GET','/thingspeak/jobs')['items'])
            result['coreOutage']={'actualProcessStopped':True,'wallSeconds':round(time.monotonic()-outage,3),
                'secondaryConfirmedWhileCoreDown':True,'automaticCoreRecovery':True,'coreMeasurements':6,'alerts':1}
    finally:
        if run.process is None:run.start_core()
        case.close()
    return result
