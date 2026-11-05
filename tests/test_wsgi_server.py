import secrets
import socket
import sqlite3
import threading
import time

import requests
import pytest

from edge_api.config import Config
from edge_api.server import EdgeServer


@pytest.mark.parametrize('multiple',[False,True])
def test_wsgi_close_during_poll_does_not_raise_socket_error(tmp_path,multiple):
    from types import SimpleNamespace
    entered=threading.Event();closed=threading.Event();errors=[]
    instance=EdgeServer.__new__(EdgeServer)
    instance._lock=threading.Lock();instance._closed=False
    instance._stopping=threading.Event();instance._running=False
    instance.app=SimpleNamespace(config={'services':SimpleNamespace(runtime=SimpleNamespace(stop=lambda:None))})
    def poll(*args,**kwargs):
        entered.set()
        if closed.wait(.2):raise OSError(10038,'socket closed while select was active')
    class SocketDouble:
        def __init__(self):self._map={};self.task_dispatcher=SimpleNamespace(shutdown=lambda **k:None)
        def run(self):poll()
        def close(self):closed.set()
    instance.server=SocketDouble()
    if multiple:
        instance.server.map=instance.server._map;del instance.server._map
    from edge_api import server as module
    from unittest.mock import patch
    def run():
        try:instance.run()
        except BaseException as error:errors.append(type(error).__name__)
    with patch.object(module.wasyncore,'loop',poll),patch.object(module.wasyncore,'close_all',lambda m:closed.set()):
        thread=threading.Thread(target=run);thread.start();assert entered.wait(1)
        instance.close();thread.join(2)
    assert not thread.is_alive() and errors==[]


def test_wsgi_stops_cleanly_and_restarts_with_same_pending_identity(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    config=Config(database_path=str(tmp_path/'wsgi.db'),admin_key=secrets.token_urlsafe(32),port=port,
        enable_availability_loop=False,delivery_poll_seconds=.02,delivery_backoff_seconds=.02)
    base=f'http://127.0.0.1:{port}/api/v1/edge'
    admin={'X-Edge-Admin-Key':config.admin_key}
    server=EdgeServer(config);thread=threading.Thread(target=server.run);thread.start()
    try:
        issued=requests.post(base+'/sensor-nodes',headers=admin,json={'deviceCode':'wsgi-node'},timeout=3)
        assert issued.status_code==201
        node={'X-Node-Credential':issued.json()['nodeCredential']}
        body={'deviceCode':'wsgi-node','sampleId':'wsgi-1','temperature':5,'humidity':50}
        original=requests.post(base+'/readings',headers=node,json=body,timeout=3)
        assert original.status_code==201 and original.json()['delivery']=='PENDING'
    finally:
        server.close();thread.join(4)
    assert not thread.is_alive() and not server.app.config['services'].runtime.thread.is_alive()
    with sqlite3.connect(config.database_path) as db:
        assert db.execute('SELECT count(*) FROM local_buffer_entries').fetchone()[0]==1
    second=EdgeServer(config)
    class Confirmed:
        def publish_batch(self,readings):return True
        def publish_reading(self,reading):return True
    second.app.config['services'].sync.cloud=Confirmed()
    thread=threading.Thread(target=second.run);thread.start()
    try:
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if requests.get(base+'/buffer',headers=admin,timeout=3).json()['pendingCount']==0:break
            time.sleep(.02)
        replay=requests.post(base+'/readings',headers=node,json=body,timeout=3)
        assert replay.json()['id']==original.json()['id'] and replay.json()['sampleId']=='wsgi-1'
        assert replay.json()['status']=='SYNCED'
    finally:
        second.close();thread.join(4)
    assert not thread.is_alive() and not second.app.config['services'].runtime.thread.is_alive()
    with socket.socket() as check:
        check.settimeout(.5);assert check.connect_ex(('127.0.0.1',port))!=0
