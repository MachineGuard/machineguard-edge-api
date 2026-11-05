"""Real SQLite contention must not strand the process mutex."""
import sqlite3
import time

import pytest
from peewee import OperationalError

from edge_api.domain.commands import CaptureSensorReadingCommand
from edge_api.infrastructure.models import database, LocalBufferEntryModel, SensorReadingModel
from conftest import post_reading


@pytest.mark.parametrize('outcome',['confirmed','unconfirmed','transport_error'])
def test_worker_recovers_in_same_process_after_completion_and_cleanup_fail(make_client,cloud,outcome):
    client=make_client(delivery_http_timeout_seconds=.1,delivery_lease_seconds=1.5,
                       delivery_poll_seconds=.02,delivery_backoff_seconds=.05)
    client.post('/api/v1/edge/sensor-nodes',json={'deviceCode':'esp32-01'})
    services=client.client.application.config['services']
    path=services.config.database_path
    original_timeout=database.timeout
    database.timeout=.1
    writer=None;calls=[]
    def send(reading):
        nonlocal writer
        calls.append(reading.sample_id)
        writer=sqlite3.connect(path)
        writer.execute('BEGIN IMMEDIATE')
        if outcome=='transport_error':raise RuntimeError('Synthetic transport failure')
        return outcome=='confirmed'
    cloud.publish_reading=send
    def replay(readings):
        calls.extend(r.sample_id for r in readings)
        return True
    cloud.publish_batch=replay
    try:
        with pytest.raises(OperationalError,match='locked'):
            services.capture.handle(CaptureSensorReadingCommand('esp32-01',5,50,sample_id='contention-1'))
        assert LocalBufferEntryModel.select().count()==1
        assert SensorReadingModel.get().status=='CALIBRATED'
        assert not services.sync.lock.locked()
        writer.rollback();writer.close();writer=None
        database.execute_sql('CREATE TABLE recovered_writer(value INTEGER)')
        original=SensorReadingModel.get()
        services.config.enable_delivery_worker=True
        services.runtime.start()
        deadline=time.monotonic()+4
        while time.monotonic()<deadline and LocalBufferEntryModel.select().count():time.sleep(.02)
        assert LocalBufferEntryModel.select().count()==0
        assert SensorReadingModel.get().sample_id==original.sample_id
        assert SensorReadingModel.get().captured_at==original.captured_at
        assert SensorReadingModel.get().calibrated_temperature==original.calibrated_temperature
        assert SensorReadingModel.get().status=='SYNCED'
        assert calls==['contention-1','contention-1']
        assert client.post('/api/v1/edge/buffer/sync').json.get('busy',False) is False
        cloud.publish_reading=lambda reading:True
        post_reading(client)
    finally:
        if writer is not None:writer.rollback();writer.close()
        services.runtime.stop()
        database.timeout=original_timeout


def test_cleanup_failure_does_not_mask_original_failure_or_hold_lock(client,monkeypatch):
    services=client.client.application.config['services']
    from edge_api.domain.commands import SyncLocalBufferCommand
    def failure():raise ValueError('original attempt failed')
    services.sync._lease=failure
    from edge_api.infrastructure.models import DeliveryStateModel
    monkeypatch.setattr(DeliveryStateModel,'update',lambda **kwargs: (_ for _ in ()).throw(OperationalError('synthetic cleanup failure')))
    with pytest.raises(ValueError,match='original attempt failed'):
        services.sync.handle(SyncLocalBufferCommand(1))
    assert not services.sync.lock.locked()
