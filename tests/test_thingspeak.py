"""Secondary delivery behavior; the base must fail assertions, not missing imports."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import post_reading
from edge_api.infrastructure.models import database, SensorReadingModel, LocalBufferEntryModel


class SecondaryDouble:
    def __init__(self, state='CONFIRMED', code='ENTRY_ACCEPTED', entry_id=12, retry_after=0):
        self.result=SimpleNamespace(state=state,code=code,entry_id=entry_id,retry_after=retry_after,http_status=200)
        self.calls=[]

    def publish(self, key, payload, cancelled):
        assert not database.in_transaction()
        self.calls.append(dict(payload))
        return self.result


@pytest.fixture
def secondary(monkeypatch,make_client,clock):
    for name,value in {'THINGSPEAK_ENABLED':'true','THINGSPEAK_DEVICE_CODE':'selected',
                       'THINGSPEAK_CHANNEL_ID':'123','THINGSPEAK_WRITE_KEY':'synthetic-secondary-only',
                       'THINGSPEAK_POLL_SECONDS':'60'}.items():monkeypatch.setenv(name,value)
    client=make_client()
    response=client.get('/api/v1/edge/thingspeak/jobs')
    services=client.client.application.config['services']
    if response.status_code==200:
        services.thingspeak.client=SecondaryDouble()
        services.thingspeak.monotonic=lambda:clock().timestamp()
    for node in ('selected','other'):
        assert client.post('/api/v1/edge/sensor-nodes',json={'deviceCode':node,'samplingIntervalSeconds':10}).status_code==201
    return client,services


def jobs(client):return client.get('/api/v1/edge/thingspeak/jobs').json['items']


def test_disabled_secondary_reports_unknown_not_delivery(client):
    data=client.get('/api/v1/edge/health').json
    assert 'thingspeak' in data, 'Secondary configuration/attempt state must be explicit'
    assert data['thingspeak']['enabled'] is False
    assert data['thingspeak']['lastConfirmedAt'] is None
    assert data['thingspeak']['code']=='DISABLED'


def test_frozen_calibrated_window_selection_and_restart(secondary,clock,cloud):
    client,s=secondary
    assert client.get('/api/v1/edge/thingspeak/jobs').status_code==200
    s.thingspeak.client=SecondaryDouble('PENDING','ZERO_ENTRY',None)
    client.put('/api/v1/edge/calibration/selected/offset',json={'temperatureOffset':1,'humidityOffset':2})
    post_reading(client,5,50,device='selected');s.thingspeak.cycle()
    clock.advance(seconds=10)
    post_reading(client,7,54,device='selected');post_reading(client,30,90,device='other');s.thingspeak.cycle()
    clock.advance(seconds=10);s.thingspeak.cycle()
    row=jobs(client)[0];frozen=dict(row['payload'])
    assert row['count']==2 and frozen['field1']=='7.0' and frozen['field2']=='54.0'
    assert len(row['sourceIds'])==2 and row['channelId']=='123' and row['deviceCode']=='selected'
    assert row['state']=='PENDING' and len(s.thingspeak.client.calls)==1
    client.put('/api/v1/edge/calibration/selected/offset',json={'temperatureOffset':9,'humidityOffset':9})
    s.thingspeak.cycle();assert len(s.thingspeak.client.calls)==1
    # A new coordinator shares durable rate/queue state, not the previous mutex.
    from edge_api.application.thingspeak import ThingSpeakDelivery
    replacement=ThingSpeakDelivery(s.config,clock,monotonic=lambda:clock().timestamp());replacement.client=SecondaryDouble()
    replacement.cycle();assert replacement.client.calls==[]
    clock.advance(seconds=20);replacement.cycle()
    assert replacement.client.calls==[frozen] and jobs(client)[0]['state']=='CONFIRMED'
    assert len(cloud.published)==3 and LocalBufferEntryModel.select().count()==0


@pytest.mark.parametrize('state,code,retry_after',[
    ('UNCERTAIN','NETWORK_ERROR',0),('UNCERTAIN','TIMEOUT',0),('UNCERTAIN','HTTP_500',0),
    ('UNCERTAIN','INVALID_BODY',0),('PENDING','ZERO_ENTRY',0),('PENDING','HTTP_429',45),
    ('REJECTED','HTTP_403',0),('REJECTED','REDIRECT_REFUSED',0)])
def test_secondary_failures_never_release_or_block_primary(secondary,clock,cloud,state,code,retry_after):
    client,s=secondary
    assert client.get('/api/v1/edge/thingspeak/jobs').status_code==200
    s.thingspeak.client=SecondaryDouble(state,code,None,retry_after)
    cloud.online=False
    for _ in range(2):
        assert post_reading(client,25,80,device='selected').status_code==201
        s.thingspeak.cycle();clock.advance(seconds=10)
    s.thingspeak.cycle()
    assert jobs(client)[0]['state']==state
    assert LocalBufferEntryModel.select().count()==2  # Even a secondary receipt cannot confirm Core.
    cloud.online=True
    assert client.post('/api/v1/edge/buffer/sync').json['pending']==0
    assert len(cloud.batches)==1 and SensorReadingModel.select().count()==2
    clock.advance(seconds=20);s.thingspeak.cycle()
    if state=='UNCERTAIN' or state=='REJECTED' or retry_after>20:
        assert len(s.thingspeak.client.calls)==1
    health=client.get('/api/v1/edge/health').json['thingspeak']
    assert 'synthetic-secondary-only' not in json.dumps(health)


def test_slow_secondary_does_not_hold_sqlite_or_core_mutex(secondary,clock,cloud):
    client,s=secondary
    assert client.get('/api/v1/edge/thingspeak/jobs').status_code==200
    entered=threading.Event();release=threading.Event();errors=[]
    post_reading(client,device='selected');s.thingspeak.cycle();clock.advance(seconds=20)
    def slow(*args):
        assert not database.in_transaction();entered.set();assert release.wait(3)
        return SimpleNamespace(state='UNCERTAIN',code='TIMEOUT',entry_id=None,retry_after=0,http_status=None)
    s.thingspeak.client.publish=slow
    def run():
        try:s.thingspeak.cycle()
        except BaseException as error:errors.append(error)
        finally:
            if not database.is_closed():database.close()
    thread=threading.Thread(target=run);thread.start()
    try:
        assert entered.wait(1);start=time.monotonic()
        assert post_reading(client,device='selected').status_code==201
        assert time.monotonic()-start<1 and len(cloud.published)==2
        assert s.thingspeak.cycle() is False
    finally:release.set();thread.join(4)
    assert not errors


def test_uncertain_job_requires_explicit_reconciliation(secondary,clock):
    client,s=secondary
    assert client.get('/api/v1/edge/thingspeak/jobs').status_code==200
    s.thingspeak.client=SecondaryDouble('UNCERTAIN','TIMEOUT',None)
    post_reading(client,device='selected');s.thingspeak.cycle();clock.advance(seconds=20);s.thingspeak.cycle()
    row=jobs(client)[0];clock.advance(seconds=100);s.thingspeak.cycle()
    assert len(s.thingspeak.client.calls)==1
    path='/api/v1/edge/thingspeak/jobs/'+row['id']+'/reconcile'
    assert client.post(path,json={'action':'retry'}).status_code==400
    assert client.post(path,json={'action':'retry','acceptDuplicateRisk':True}).status_code==202
    assert jobs(client)[0]['payload']==row['payload']
    s.thingspeak.client=SecondaryDouble();s.thingspeak.cycle()
    assert jobs(client)[0]['state']=='CONFIRMED'
    assert any(a['code']=='ADMIN_RETRY_DUPLICATE_RISK' for a in jobs(client)[0]['attempts'])


def test_secondary_overflow_preserves_primary_and_reason(secondary,clock,cloud):
    client,s=secondary
    assert client.get('/api/v1/edge/thingspeak/jobs').status_code==200
    s.config.thingspeak_capacity=1
    s.thingspeak.client=SecondaryDouble('UNCERTAIN','TIMEOUT',None)
    for _ in range(3):
        post_reading(client,device='selected');s.thingspeak.cycle();clock.advance(seconds=20)
    s.thingspeak.cycle()
    states=[r['state'] for r in jobs(client)]
    assert states.count('UNCERTAIN')==1 and states.count('DROPPED_OVERFLOW')==2
    assert all(r['code']=='QUEUE_CAPACITY' for r in jobs(client) if r['state']=='DROPPED_OVERFLOW')
    assert SensorReadingModel.select().count()==3 and len(cloud.published)==3


def test_node_cannot_query_or_reconcile_secondary(secondary):
    client,s=secondary
    for method,path in [('GET','/api/v1/edge/thingspeak/jobs'),('POST','/api/v1/edge/thingspeak/jobs/none/reconcile')]:
        assert client.open(path,method,headers={'X-Node-Credential':client.tokens['selected']},json={}).status_code==403
