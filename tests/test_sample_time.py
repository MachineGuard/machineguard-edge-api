"""Sample identity, capture time and SQLite upgrade regressions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import secrets
import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from edge_api import create_app
from edge_api.config import Config
from edge_api.infrastructure.models import database

BASE = '/api/v1/edge'


@pytest.fixture
def sample(tmp_path, cloud, clock):
    key = secrets.token_urlsafe(32)
    config = Config(database_path=str(tmp_path/'samples.db'), admin_key=key, enable_availability_loop=False,enable_delivery_worker=False)
    app = create_app(config, cloud, clock)
    c = app.test_client(); admin = {'X-Edge-Admin-Key': key}; tokens = {}
    for code in ['node-a', 'node-b']:
        r = c.post(BASE+'/sensor-nodes', headers=admin, json={'deviceCode': code})
        assert r.status_code == 201
        tokens[code] = r.json['nodeCredential']
    yield SimpleNamespace(c=c, app=app, config=config, admin=admin, tokens=tokens)
    if not database.is_closed(): database.close()


def send(s, sample_id='bootA-1', code='node-a', **fields):
    return s.c.post(BASE+'/readings', headers={'X-Node-Credential': s.tokens[code]},
        json={'deviceCode': code, 'sampleId': sample_id, 'temperature': 5, 'humidity': 50, **fields})


def rows(s):
    return s.c.get(BASE+'/readings', headers=s.admin).json


def test_sample_retry_without_clock_keeps_original_row_capture_and_calibration(sample, cloud, clock):
    first = send(sample); assert first.status_code == 201
    clock.advance(seconds=10)
    sample.c.put(BASE+'/calibration/node-a/offset', headers=sample.admin,
                 json={'temperatureOffset': 1, 'humidityOffset': 0})
    again = send(sample)
    assert again.status_code == 201
    assert again.json == first.json
    assert len(rows(sample)) == 1 and len(cloud.published) == 1


def test_sample_retry_persists_across_restart_and_does_not_restore_offline(sample, cloud, clock):
    cloud.online = False
    first = send(sample)
    clock.advance(seconds=76)
    sample.c.get(BASE+'/sensor-nodes/offline', headers=sample.admin)
    sample.c = create_app(sample.config, cloud, clock).test_client()
    again = send(sample)
    assert again.json['id'] == first.json['id']
    assert sample.c.get(BASE+'/buffer', headers=sample.admin).json['pendingCount'] == 1
    assert 'node-a' in [r['deviceCode'] for r in sample.c.get(BASE+'/sensor-nodes/offline', headers=sample.admin).json]


@pytest.mark.parametrize('changes', [{'temperature': 6}, {'humidity': 51}, {'capturedAt': '2026-10-01T12:00:00Z'}])
def test_sample_conflict_preserves_original(sample, changes):
    first = send(sample)
    conflict = send(sample, **changes)
    assert conflict.status_code == 409
    assert rows(sample) == [first.json]


def test_sample_timestamp_retry_normalizes_offset_but_rejects_a_changed_instant(sample):
    first = send(sample, capturedAt='2026-10-01T07:00:00-05:00')
    same = send(sample, capturedAt='2026-10-01T12:00:00Z')
    assert same.json['id'] == first.json['id']
    assert send(sample, capturedAt='2026-10-01T12:00:01Z').status_code == 409


def test_sample_identity_is_per_node_and_per_boot(sample):
    a = send(sample); b = send(sample, code='node-b'); reboot = send(sample, sample_id='bootB-1')
    assert len({r.json['id'] for r in [a,b,reboot]}) == 3
    assert a.json.get('sampleId') == 'bootA-1'


@pytest.mark.parametrize('value', [None, '', 1, True, 'no-sequence', 'boot-01', '-1', 'a'*100+'-1', 'boot-1\n'])
def test_sample_id_requires_a_bounded_boot_and_sequence(sample, value):
    assert send(sample, sample_id=value).status_code == 400
    assert rows(sample) == []


def test_sample_id_is_required(sample):
    r = sample.c.post(BASE+'/readings', headers={'X-Node-Credential':sample.tokens['node-a']},
                     json={'deviceCode':'node-a','temperature':5,'humidity':50})
    assert r.status_code == 400


@pytest.mark.parametrize('captured', ['2026-10-01T12:00:00', '2026-10-01', None, '1970-01-01T00:00:00Z',
                                    '2026-08-31T12:00:00Z', '2026-10-01T12:01:01Z'])
def test_invalid_capture_is_rejected_with_reason_and_no_side_effects(sample, cloud, captured):
    r = send(sample, capturedAt=captured)
    assert r.status_code == 400 and r.json['error']
    assert rows(sample) == [] and cloud.published == []
    assert sample.c.get(BASE+'/buffer', headers=sample.admin).json['pendingCount'] == 0


@pytest.mark.parametrize('seconds', [50, 60, -30*86400])
def test_capture_accepts_inclusive_time_boundaries(sample, clock, seconds):
    captured = (clock.now+timedelta(seconds=seconds)).isoformat()+'Z'
    assert send(sample, capturedAt=captured).status_code == 201


def test_time_policy_is_configurable(sample, monkeypatch, cloud, clock):
    monkeypatch.setenv('EDGE_MAX_FUTURE_SECONDS','5')
    monkeypatch.setenv('EDGE_MAX_READING_AGE_DAYS','1')
    monkeypatch.setenv('EDGE_MIN_CAPTURED_AT','2026-09-01T00:00:00Z')
    sample.c = create_app(Config(database_path=sample.config.database_path, admin_key=sample.config.admin_key,
                                enable_availability_loop=False,enable_delivery_worker=False),cloud,clock).test_client()
    assert send(sample,capturedAt=(clock.now+timedelta(seconds=6)).isoformat()+'Z').status_code == 400
    assert send(sample,capturedAt=(clock.now-timedelta(days=2)).isoformat()+'Z').status_code == 400
    assert send(sample,capturedAt=(clock.now+timedelta(seconds=5)).isoformat()+'Z').status_code == 201


def test_minimum_capture_is_configurable_independently_of_age(sample, monkeypatch, cloud, clock):
    monkeypatch.setenv('EDGE_MIN_CAPTURED_AT','2026-10-01T11:59:59Z')
    sample.c = create_app(Config(database_path=sample.config.database_path, admin_key=sample.config.admin_key,
                                enable_availability_loop=False,enable_delivery_worker=False),cloud,clock).test_client()
    assert send(sample,capturedAt='2026-10-01T11:59:58Z').status_code == 400
    assert send(sample,capturedAt='2026-10-01T11:59:59Z').status_code == 201


def test_known_retry_is_confirmed_after_time_window_expires(sample, clock):
    first = send(sample,capturedAt=clock.now.isoformat()+'Z')
    clock.advance(days=31)
    again = send(sample,capturedAt=first.json['capturedAt'])
    assert again.json['id'] == first.json['id'] and len(rows(sample)) == 1


def test_concurrent_retries_create_one_row_and_one_publish(sample, cloud):
    def post(_):
        c = sample.app.test_client()
        r = c.post(BASE+'/readings', headers={'X-Node-Credential':sample.tokens['node-a']},
                   json={'deviceCode':'node-a','sampleId':'concurrent-1','temperature':5,'humidity':50})
        if not database.is_closed(): database.close()
        return r.status_code,r.json['id']
    with ThreadPoolExecutor(4) as pool: results = list(pool.map(post,range(4)))
    assert all(status == 201 for status,_ in results)
    assert len({identity for _,identity in results}) == 1
    assert len(rows(sample)) == 1 and len(cloud.published) == 1


@pytest.fixture
def legacy_v1(tmp_path, clock):
    from edge_api.infrastructure.models import (CalibrationProfileModel, LocalBufferEntryModel, OfflineNodeModel,
        SyncStateModel, NodeCredentialModel, CalibrationChangeModel)
    path = str(tmp_path/'legacy-v1.db')
    if not database.is_closed(): database.close()
    database.init(path); database.connect()
    database.create_tables([CalibrationProfileModel])
    database.execute_sql('''CREATE TABLE sensor_readings (
        id INTEGER PRIMARY KEY, sensor_node_id TEXT NOT NULL, calibration_profile_id INTEGER,
        raw_temperature REAL NOT NULL, raw_humidity REAL NOT NULL, calibrated_temperature REAL,
        calibrated_humidity REAL, captured_at DATETIME NOT NULL, status TEXT NOT NULL,
        FOREIGN KEY (calibration_profile_id) REFERENCES calibration_profiles(id))''')
    database.create_tables([LocalBufferEntryModel, OfflineNodeModel, SyncStateModel, NodeCredentialModel, CalibrationChangeModel])
    profile = CalibrationProfileModel.create(sensor_node_id='legacy',temperature_offset=0,humidity_offset=0,
        temperature_min=-40,temperature_max=80,humidity_min=0,humidity_max=100,sampling_interval_seconds=60,
        calibrated_at=clock.now,updated_at=clock.now)
    for identity,status in [(11,'CALIBRATED'),(12,'DISCARDED'),(13,'SYNCED')]:
        database.execute_sql('INSERT INTO sensor_readings VALUES (?,?,?,?,?,?,?,?,?)',
            (identity,'legacy',profile.id,5,50,5,50,clock.now,status))
    LocalBufferEntryModel.create(sensor_reading=11,gateway_id='old',enqueued_at=clock.now)
    OfflineNodeModel.create(sensor_node_id='legacy',last_seen_at=clock.now,expected_sampling_interval=60,detected_at=clock.now)
    SyncStateModel.create(gateway_id='old',last_sync_at=clock.now)
    token = 'mgn_'+secrets.token_urlsafe(32)
    NodeCredentialModel.create(profile=profile,credential_hash=hashlib.sha256(token.encode()).hexdigest(),issued_at=clock.now)
    CalibrationChangeModel.create(profile=profile,actor='legacy-operator',changed_at=clock.now,
        old_temperature_offset=0,old_humidity_offset=0,new_temperature_offset=0,new_humidity_offset=0)
    database.pragma('user_version',1)
    columns = {t:[c.name for c in database.get_columns(t)] for t in database.get_tables()}
    before = {t:list(database.execute_sql('SELECT '+','.join(cs)+' FROM '+t+' ORDER BY 1')) for t,cs in columns.items()}
    config = Config(database_path=path,admin_key=secrets.token_urlsafe(32),enable_availability_loop=False,enable_delivery_worker=False)
    yield SimpleNamespace(path=path,config=config,token=token,columns=columns,before=before)
    if not database.is_closed(): database.close()


def test_v1_migration_preserves_every_row_and_credentials_then_restarts(legacy_v1, cloud, clock):
    old=legacy_v1
    c=create_app(old.config,cloud,clock).test_client()
    after={t:list(database.execute_sql('SELECT '+','.join(cs)+' FROM '+t+' ORDER BY 1')) for t,cs in old.columns.items()}
    assert after==old.before
    assert database.pragma('user_version')==3
    assert list(database.execute_sql('SELECT sample_id FROM sensor_readings'))==[(None,)]*3
    c=create_app(old.config,cloud,clock).test_client()
    r=c.post(BASE+'/readings',headers={'X-Node-Credential':old.token},
        json={'deviceCode':'legacy','sampleId':'newboot-1','temperature':5,'humidity':50})
    assert r.status_code==201
    assert c.get(BASE+'/buffer',headers={'X-Edge-Admin-Key':old.config.admin_key}).json['pendingCount']==2
    from peewee import IntegrityError
    with pytest.raises(IntegrityError):
        database.execute_sql("UPDATE sensor_readings SET sample_id='newboot-1' WHERE id=11")


def test_v1_migration_failure_rolls_back_columns_and_version(legacy_v1, monkeypatch, cloud, clock):
    original=database.execute_sql
    def fail_index(sql,*args,**kwargs):
        if 'CREATE UNIQUE INDEX' in sql and 'uq_sensor_readings_node_sample' in sql:
            raise RuntimeError('Synthetic migration failure')
        return original(sql,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(database,'execute_sql',fail_index)
        with pytest.raises(RuntimeError,match='Synthetic migration failure'):
            create_app(legacy_v1.config,cloud,clock)
    if not database.is_closed(): database.close()
    with sqlite3.connect(legacy_v1.path) as check:
        assert check.execute('PRAGMA user_version').fetchone()[0]==1
        assert 'sample_id' not in [row[1] for row in check.execute('PRAGMA table_info(sensor_readings)')]
        assert check.execute('SELECT count(*) FROM sensor_readings').fetchone()[0]==3


def test_unknown_future_schema_is_not_modified(tmp_path, cloud, clock):
    path=str(tmp_path/'future.db')
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE keep_this(value TEXT)'); c.execute("INSERT INTO keep_this VALUES ('evidence')")
        c.execute('PRAGMA user_version=99')
    with pytest.raises(ValueError,match='Unsupported'):
        create_app(Config(database_path=path,admin_key=secrets.token_urlsafe(32),enable_availability_loop=False,enable_delivery_worker=False),cloud,clock)
    if not database.is_closed(): database.close()
    with sqlite3.connect(path) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==99
        assert c.execute('SELECT value FROM keep_this').fetchone()[0]=='evidence'


@pytest.mark.parametrize('variable,value', [('EDGE_MIN_CAPTURED_AT','2026-01-01'),('EDGE_MIN_CAPTURED_AT','bad'),
    ('EDGE_MAX_FUTURE_SECONDS','-1'),('EDGE_MAX_READING_AGE_DAYS','0')])
def test_invalid_time_configuration_fails_at_startup(tmp_path, monkeypatch, variable, value):
    monkeypatch.setenv(variable,value)
    with pytest.raises(ValueError):
        create_app(Config(database_path=str(tmp_path/'invalid-config.db'),admin_key=secrets.token_urlsafe(32),
                          enable_availability_loop=False,enable_delivery_worker=False))
