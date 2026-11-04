"""Delivery contracts: durable acknowledgement and administrative recovery."""
import pytest
import time
from types import SimpleNamespace

from conftest import post_reading
from edge_api.config import Config
from edge_api.infrastructure.models import database, LocalBufferEntryModel, SensorReadingModel
from edge_api.infrastructure.cloud_client import HttpCloudClient
from edge_api import create_app
from conftest import AuthenticatedEdgeClient


def test_reading_and_outbox_are_committed_before_http(client, cloud):
    observations = []
    def send(reading):
        observations.append((database.in_transaction(), SensorReadingModel.select().count(),
                             LocalBufferEntryModel.select().count()))
        return True
    cloud.publish_reading = send
    response = post_reading(client)
    assert response.status_code == 201
    assert observations == [(False, 1, 1)]
    assert response.json['persistence'] == 'LOCAL_COMMITTED'
    assert response.json['delivery'] == 'CONFIRMED'


def test_extreme_age_is_rejected_at_startup():
    with pytest.raises(ValueError, match='time|age|AGE'):
        Config(admin_key='a' * 32, max_reading_age_days=999999).validate()


def test_reception_and_grace_prevent_premature_offline(make_client, clock):
    client = make_client()
    client.post('/api/v1/edge/sensor-nodes', json={'deviceCode': 'short', 'samplingIntervalSeconds': 10})
    post_reading(client, device='short')
    clock.advance(seconds=11)
    assert client.get('/api/v1/edge/sensor-nodes/offline').json == []
    clock.advance(seconds=2)
    assert len(client.get('/api/v1/edge/sensor-nodes/offline').json) == 1


def test_health_distinguishes_unknown_transport_and_preserves_pending(client, cloud):
    cloud.online = False
    post_reading(client)
    health = client.get('/api/v1/edge/health').json
    assert health['delivery']['pending'] == 1
    assert health['delivery']['transport'] == 'DEGRADED'
    assert health['delivery']['lastConfirmedAt'] is None
    assert client.get('/api/v1/edge/quarantine?limit=2&offset=0').json['items'] == []


@pytest.mark.parametrize('code,status,node_pause', [
    ('READING_CONFLICT',409,False), ('READING_INVALID',400,False),
    ('NODE_NOT_AUTHORIZED',404,True), ('THRESHOLD_NOT_CONFIGURED',409,True),
])
def test_partition_isolates_sample_or_pauses_node_without_global_block(client, cloud, clock, monkeypatch, code, status, node_pause):
    cloud.online = False
    client.post('/api/v1/edge/sensor-nodes',json={'deviceCode':'healthy'})
    for device in ['esp32-01','esp32-01','healthy']:
        post_reading(client, device=device)
        clock.advance(seconds=1)
    poisoned = SensorReadingModel.select().order_by(SensorReadingModel.id).first().captured_at
    calls=[]
    def post(url, json, **kwargs):
        rows=json.get('measurements',[json]); calls.append(rows)
        rejected=any(r['deviceCode']=='esp32-01' and (node_pause or r['capturedAt']==poisoned.isoformat()+'Z') for r in rows)
        return SimpleNamespace(status_code=status if rejected else 201, json=lambda: {'code':code} if rejected else {
            'readingsAccepted':len(rows),'measurementsCreated':len(rows)*2,'measurementsReused':0})
    monkeypatch.setattr('requests.post',post)
    services=client.client.application.config['services']
    services.sync.cloud=HttpCloudClient('http://127.0.0.1:1','synthetic-test')
    client.post('/api/v1/edge/buffer/sync')
    statuses=[r.status for r in SensorReadingModel.select().order_by(SensorReadingModel.id)]
    assert statuses == (['CALIBRATED','CALIBRATED','SYNCED'] if node_pause else ['QUARANTINED','SYNCED','SYNCED'])
    health=client.get('/api/v1/edge/health').json['delivery']
    assert len(health['pausedNodes']) == int(node_pause)
    assert health['quarantined'] == int(not node_pause)
    assert len(calls) <= 16


def test_worker_resumes_pending_without_manual_sync(tmp_path):
    from conftest import FakeCloud
    cloud=FakeCloud(); cloud.online=False
    config=Config(database_path=str(tmp_path/'automatic.db'),admin_key='x'*32,enable_availability_loop=False,enable_delivery_worker=False)
    config.enable_delivery_worker=True
    config.delivery_poll_seconds=0.05
    config.delivery_backoff_seconds=0.05
    app=create_app(config,cloud=cloud)
    client=AuthenticatedEdgeClient(app.test_client(),config.admin_key)
    try:
        client.post('/api/v1/edge/sensor-nodes',json={'deviceCode':'auto'})
        post_reading(client,device='auto')
        cloud.online=True
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and LocalBufferEntryModel.select().count():
            time.sleep(0.02)
        assert LocalBufferEntryModel.select().count()==0
    finally:
        if hasattr(app.config['services'],'runtime'):
            app.config['services'].runtime.stop()


def test_overflow_has_own_state_and_loss_evidence(make_client, cloud):
    cloud.online=False
    client=make_client(buffer_capacity=1)
    client.post('/api/v1/edge/sensor-nodes',json={'deviceCode':'esp32-01'})
    post_reading(client); post_reading(client)
    assert SensorReadingModel.select().order_by(SensorReadingModel.id).first().status=='DROPPED_OVERFLOW'
    assert client.get('/api/v1/edge/health').json['delivery']['lost']==1


@pytest.mark.parametrize('status,code',[(401,'READING_INVALID'),(403,'READING_CONFLICT'),
    (400,None),(200,None),(500,'READING_INVALID')])
def test_unconfirmed_receipts_and_credentials_never_release_or_quarantine(client,cloud,monkeypatch,status,code):
    cloud.online=False
    post_reading(client)
    monkeypatch.setattr('requests.post',lambda *a,**k: SimpleNamespace(status_code=status,json=lambda:{'code':code}))
    client.client.application.config['services'].sync.cloud=HttpCloudClient('http://127.0.0.1:1','synthetic-test')
    response=client.post('/api/v1/edge/buffer/sync')
    assert response.json['pending']==1
    health=client.get('/api/v1/edge/health').json['delivery']
    assert health['quarantined']==0
    assert health['transport']=='DEGRADED'
    if status in (401,403):
        assert health['credential']=='REJECTED'


def test_repeated_5xx_partitions_but_single_5xx_stays_pending(client,cloud,clock,monkeypatch):
    cloud.online=False
    for _ in range(3):
        post_reading(client); clock.advance(seconds=1)
    bad=SensorReadingModel.select().order_by(SensorReadingModel.id).first().captured_at.isoformat()+'Z'
    calls=[]
    def post(url,json,**kwargs):
        rows=json.get('measurements',[json]);calls.append(rows)
        reject=any(r['capturedAt']==bad for r in rows)
        return SimpleNamespace(status_code=500 if reject else 201,json=lambda:{'readingsAccepted':len(rows),
            'measurementsCreated':len(rows)*2,'measurementsReused':0})
    monkeypatch.setattr('requests.post',post)
    services=client.client.application.config['services']
    services.sync.cloud=HttpCloudClient('http://127.0.0.1:1','synthetic-test')
    assert client.post('/api/v1/edge/buffer/sync').json['pending']==3
    calls.clear()
    assert client.post('/api/v1/edge/buffer/sync').json['pending']==1
    assert [r.status for r in SensorReadingModel.select().order_by(SensorReadingModel.id)]==['CALIBRATED','SYNCED','SYNCED']
    assert len(calls)<=services.config.delivery_max_requests
    assert client.get('/api/v1/edge/health').json['delivery']['quarantined']==0


def test_reprocess_retains_original_calibration_and_append_only_history(client,cloud,monkeypatch):
    cloud.online=False
    post_reading(client)
    response=SimpleNamespace(status_code=409,json=lambda:{'code':'READING_CONFLICT'})
    monkeypatch.setattr('requests.post',lambda *a,**k:response)
    services=client.client.application.config['services']
    services.sync.cloud=HttpCloudClient('http://127.0.0.1:1','synthetic-test')
    client.post('/api/v1/edge/buffer/sync')
    page=client.get('/api/v1/edge/quarantine?limit=1').json
    assert page['total']==1
    original=page['items'][0]['reading']
    qid=page['items'][0]['id']
    client.put('/api/v1/edge/calibration/esp32-01/offset',json={'temperatureOffset':2,'humidityOffset':1})
    assert client.post(f'/api/v1/edge/quarantine/{qid}/reprocess').status_code==202
    assert client.post(f'/api/v1/edge/quarantine/{qid}/reprocess').status_code==409
    response.status_code=200
    response.json=lambda:{'readingsAccepted':1,'measurementsCreated':0,'measurementsReused':2}
    assert client.post('/api/v1/edge/buffer/sync').json['pending']==0
    final=client.get('/api/v1/edge/quarantine').json
    assert final['total']==1
    saved=final['items'][0]['reading']
    for key in ['id','sampleId','rawTemperature','rawHumidity','calibratedTemperature','calibratedHumidity','capturedAt','receivedAt']:
        assert saved[key]==original[key]
    assert any(a['outcome']=='REQUEUED' and a['actor']=='local-admin' for a in final['items'][0]['attempts'])
    assert client.get('/api/v1/edge/quarantine?offset=1').json['items']==[]


def test_capture_during_slow_core_does_not_hold_sqlite_or_duplicate_send(client,cloud,clock):
    import threading
    entered=threading.Event(); release=threading.Event();errors=[]
    def send(reading):
        entered.set()
        assert release.wait(3)
        return True
    cloud.publish_reading=send
    def capture():
        try:
            post_reading(client)
        except BaseException as error:
            errors.append(error)
        finally:
            if not database.is_closed():database.close()
    thread=threading.Thread(target=capture); thread.start()
    try:
        assert entered.wait(2)
        clock.advance(seconds=1)
        start=time.monotonic()
        assert post_reading(client).status_code==201
        assert time.monotonic()-start<1
        assert client.post('/api/v1/edge/buffer/sync').json.get('busy') is True
    finally:
        release.set();thread.join(4)
    assert not errors
    assert LocalBufferEntryModel.select().count()==1


def test_future_capture_and_implausible_samples_do_not_extend_availability(make_client,clock):
    from datetime import timedelta
    client=make_client()
    client.post('/api/v1/edge/sensor-nodes',json={'deviceCode':'esp32-01','samplingIntervalSeconds':10})
    post_reading(client,captured_at=clock()+timedelta(seconds=50))
    clock.advance(seconds=13)
    assert len(client.get('/api/v1/edge/sensor-nodes/offline').json)==1
    post_reading(client,temperature=999)
    assert len(client.get('/api/v1/edge/sensor-nodes/offline').json)==1


def test_v2_migration_preserves_identity_credentials_pending_and_is_repeatable(client,cloud):
    import sqlite3
    from edge_api.infrastructure.models import init_database
    cloud.online=False
    post_reading(client)
    path=client.client.application.config['services'].config.database_path
    database.close()
    # A populated v2 database has no v3 delivery metadata or reception timestamp.
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE sensor_readings DROP COLUMN received_at')
        for column in ['attempts','next_attempt_at','inflight_until']:
            connection.execute('ALTER TABLE local_buffer_entries DROP COLUMN '+column)
        for table in ['deliveryattemptmodel','quarantinemodel','deliverylossmodel','nodedeliverystatemodel','deliverystatemodel']:
            connection.execute('DROP TABLE '+table)
        connection.execute('PRAGMA user_version=2')
        before={t:connection.execute('SELECT * FROM '+t).fetchall() for t in ['sensor_readings','local_buffer_entries','node_credentials','calibration_changes']}
    init_database(path)
    assert database.pragma('user_version')==3
    with sqlite3.connect(path) as connection:
        for table,rows in before.items():
            columns=connection.execute('PRAGMA table_info('+table+')').fetchall()
            # New metadata is appended, original columns and every value stay in order.
            count=len(rows[0]) if rows else len(columns)
            assert [r[:count] for r in connection.execute('SELECT * FROM '+table).fetchall()]==rows
    init_database(path)
    assert client.post('/api/v1/edge/buffer/sync').json['pending']==1


def test_slow_worker_does_not_delay_availability_checks(tmp_path,clock):
    import threading
    from conftest import FakeCloud
    cloud=FakeCloud();cloud.online=False
    config=Config(database_path=str(tmp_path/'availability.db'),admin_key='x'*32,
                  enable_availability_loop=False,enable_delivery_worker=False,
                  delivery_poll_seconds=.01,availability_check_seconds=.02)
    first=create_app(config,cloud,clock)
    client=AuthenticatedEdgeClient(first.test_client(),config.admin_key)
    client.post('/api/v1/edge/sensor-nodes',json={'deviceCode':'esp32-01','samplingIntervalSeconds':10})
    post_reading(client)
    clock.advance(seconds=13)
    entered=threading.Event();release=threading.Event()
    def send(readings):
        entered.set();assert release.wait(3);return False
    cloud.publish_batch=send
    config.enable_delivery_worker=True;config.enable_availability_loop=True
    from edge_api.infrastructure.models import OfflineNodeModel
    # The persisted retry is due; the worker starts before the first availability tick.
    LocalBufferEntryModel.update(next_attempt_at=None).execute()
    second=create_app(config,cloud,clock)
    try:
        assert entered.wait(1)
        deadline=time.monotonic()+.4
        while time.monotonic()<deadline and not OfflineNodeModel.select().exists():time.sleep(.01)
        assert OfflineNodeModel.select().exists()
    finally:
        release.set();second.config['services'].runtime.stop()
