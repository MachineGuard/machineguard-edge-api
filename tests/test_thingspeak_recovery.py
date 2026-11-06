import json
import sqlite3
from datetime import timedelta

from conftest import post_reading
from test_thingspeak import secondary,jobs,SecondaryDouble
from edge_api.infrastructure.models import (database,init_database,THINGSPEAK_MODELS,
    ThingSpeakJobModel as Job,ThingSpeakChannelModel as Channel,SensorReadingModel)


def test_expired_started_attempt_is_uncertain_never_replayed(secondary,clock):
    client,s=secondary
    post_reading(client,device='selected');s.thingspeak._plan(clock())
    clock.advance(seconds=20);s.thingspeak._plan(clock())
    reserved=s.thingspeak._reserve(clock())
    assert reserved.state=='SENDING'
    from edge_api.application.thingspeak import ThingSpeakDelivery
    other=ThingSpeakDelivery(s.config,clock,monotonic=lambda:clock().timestamp());other.client=SecondaryDouble()
    other.cycle();assert other.client.calls==[]
    clock.advance(seconds=11);other.cycle()
    assert other.client.calls==[] and jobs(client)[0]['state']=='UNCERTAIN'
    assert jobs(client)[0]['code']=='WORKER_LOST'
    assert client.get('/api/v1/edge/health').json['thingspeak']['uncertain']==1


def test_late_and_window_capacity_losses_are_queryable(secondary,clock):
    client,s=secondary;s.config.thingspeak_max_samples=1
    post_reading(client,device='selected');s.thingspeak.cycle()
    clock.advance(seconds=5);post_reading(client,device='selected');s.thingspeak.cycle()
    clock.advance(seconds=15);s.thingspeak.cycle()
    frozen=jobs(client)[0]['payload']
    clock.advance(seconds=-10);post_reading(client,device='selected');s.thingspeak.cycle()
    losses=client.get('/api/v1/edge/thingspeak/losses').json
    assert [r['code'] for r in losses['items']]==['WINDOW_CAPACITY','LATE_AFTER_FREEZE']
    assert jobs(client)[0]['payload']==frozen and SensorReadingModel.select().count()==3
    assert client.get('/api/v1/edge/thingspeak/jobs?offset=1').json['items']==[]
    assert client.get('/api/v1/edge/thingspeak/jobs?state=CONFIRMED').json['total']==1
    assert client.get('/api/v1/edge/thingspeak/jobs?offset=-1').status_code==400
    assert client.get('/api/v1/edge/thingspeak/jobs?state=made-up').status_code==400


def test_channel_binding_never_mixes_another_node_or_window(secondary,clock):
    client,s=secondary
    from edge_api.application.thingspeak import ThingSpeakDelivery
    s.config.thingspeak_device_code='other'
    other=ThingSpeakDelivery(s.config,clock)
    assert other.code=='CHANNEL_BINDING_CONFLICT' and other.cycle() is False
    assert Channel.get_by_id('123').device_code=='selected'


def test_missing_or_invalid_optional_configuration_does_not_stop_capture(monkeypatch,make_client):
    monkeypatch.setenv('THINGSPEAK_ENABLED','true');monkeypatch.setenv('THINGSPEAK_WRITE_KEY','')
    client=make_client()
    assert client.get('/api/v1/edge/health').json['thingspeak']['configuration']=='MISSING_CONFIGURATION'
    client.post('/api/v1/edge/sensor-nodes',json={'deviceCode':'selected'})
    assert post_reading(client,device='selected').status_code==201
    monkeypatch.setenv('THINGSPEAK_WRITE_KEY','synthetic-secondary-only')
    monkeypatch.setenv('THINGSPEAK_CHANNEL_ID','123');monkeypatch.setenv('THINGSPEAK_DEVICE_CODE','selected')
    monkeypatch.setenv('THINGSPEAK_WINDOW_SECONDS','NaN')
    client=make_client()
    assert client.get('/api/v1/edge/health').json['thingspeak']['configuration']=='INVALID_CONFIGURATION'
    assert post_reading(client,device='selected').status_code==201


def test_populated_v3_additive_upgrade_is_repeatable_preserves_all_primary_evidence(client,cloud):
    cloud.online=False
    first=post_reading(client)
    path=client.client.application.config['services'].config.database_path
    for model in reversed(THINGSPEAK_MODELS):model.drop_table()
    database.pragma('user_version',3);database.close()
    with sqlite3.connect(path) as connection:
        tables=[row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before={table:connection.execute('SELECT * FROM "'+table+'"').fetchall() for table in tables}
    init_database(path);init_database(path)
    assert database.pragma('user_version')==4
    with sqlite3.connect(path) as connection:
        assert {table:connection.execute('SELECT * FROM "'+table+'"').fetchall() for table in tables}==before
    assert client.get('/api/v1/edge/buffer').json['pendingCount']==1
    assert client.post('/api/v1/edge/buffer/sync').json['pending']==1
    assert SensorReadingModel.get_by_id(first.json['id']).sample_id==first.json['sampleId']


def test_wall_clock_jump_cannot_burst_secondary_attempts(secondary,clock):
    client,s=secondary;s.thingspeak.client=SecondaryDouble('PENDING','ZERO_ENTRY',None)
    mono=[100.0];s.thingspeak.monotonic=lambda:mono[0]
    post_reading(client,device='selected');s.thingspeak.cycle()
    clock.advance(seconds=20);s.thingspeak.cycle();assert len(s.thingspeak.client.calls)==1
    clock.advance(days=1);s.thingspeak.cycle();assert len(s.thingspeak.client.calls)==1
    mono[0]+=20;s.thingspeak.cycle();assert len(s.thingspeak.client.calls)==2
