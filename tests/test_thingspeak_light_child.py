"""Spawn child stays free of the Edge application stack; TIMEOUT diagnostics are sanitized. No network."""
import json
import subprocess
import sys
import time
from pathlib import Path

from edge_api.infrastructure import thingspeak_client as client_module
from thingspeak_child import worker
from test_thingspeak_observation import observed, phase, wire_client

ROOT = Path(__file__).resolve().parents[1]
HEAVY = ('edge_api', 'flask', 'waitress', 'peewee')
SECRET = 'synthetic-private-key'
PAYLOAD_MARK = 'synthetic-payload-marker'
TIMEOUT_KEYS = {'childAlive', 'exitCode', 'lastMarker', 'markerBasis', 'msSinceLastMarker'}


def heavy_modules_after(code):
    script = (f'import json,sys\nsys.path.insert(0,{str(ROOT)!r})\n{code}\n'
              f'print(json.dumps(sorted(m for m in sys.modules if m.split(".")[0] in {HEAVY!r})))')
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_child_module_import_does_not_load_application_stack():
    assert heavy_modules_after('import thingspeak_child.worker') == []


def test_spawn_reimport_of_run_script_does_not_load_application_stack():
    # spawn re-runs the parent's main script as __mp_main__ before it unpickles the target.
    assert heavy_modules_after(f"import runpy\nrunpy.run_path({str(ROOT / 'run.py')!r},run_name='__mp_main__')") == []


def test_spawn_target_and_default_request_are_resolved_outside_edge_api():
    assert client_module._child.__module__ == 'thingspeak_child.worker'
    assert client_module.http_exchange.__module__ == 'thingspeak_child.worker'
    assert client_module.ThingSpeakResult is worker.ThingSpeakResult


def test_timeout_before_any_marker_records_child_state_only(monkeypatch, caplog):
    client = wire_client(monkeypatch, [], seconds=.3)
    result = client.publish(SECRET, {'field1': PAYLOAD_MARK})
    assert result.code == 'TIMEOUT'
    timeout = observed(client)['timeout']
    assert set(timeout) == TIMEOUT_KEYS
    assert timeout['childAlive'] is True and timeout['exitCode'] is None
    assert timeout['lastMarker'] is None and timeout['markerBasis'] == 'START_RETURNED'
    assert 250 <= timeout['msSinceLastMarker'] < 1500
    text = json.dumps(observed(client)) + caplog.text
    assert 'ThingSpeak diagnostic' in caplog.text and '"timeout":{' in caplog.text
    assert SECRET not in text and PAYLOAD_MARK not in text and 'THINGSPEAKAPIKEY' not in text


def test_timeout_after_marker_names_last_child_phase(monkeypatch):
    client = wire_client(monkeypatch, [(0, phase('CHILD_ENTERED')), (.02, phase('HTTP_STARTED'))], seconds=.3)
    result = client.publish(SECRET, {})
    assert result.code == 'TIMEOUT'
    timeout = observed(client)['timeout']
    assert set(timeout) == TIMEOUT_KEYS
    assert timeout['lastMarker'] == 'HTTP_STARTED' and timeout['markerBasis'] == 'CHILD'
    assert timeout['childAlive'] is True and timeout['exitCode'] is None
    assert 0 <= timeout['msSinceLastMarker'] < 350


def test_completed_attempt_has_no_timeout_snapshot(monkeypatch):
    frame = {'kind': 'result', 'result': {'state': 'CONFIRMED', 'code': 'ENTRY_ACCEPTED', 'entryId': 7,
                                          'retryAfter': 0, 'httpStatus': 200}}
    client = wire_client(monkeypatch, [(0, frame)])
    assert client.publish(SECRET, {}).state == 'CONFIRMED'
    assert observed(client)['timeout'] is None


def sleeping_request(key, payload, seconds):
    # Module-level so the real spawn child can unpickle it. It outlives the parent's budget.
    time.sleep(30)
    return worker.ThingSpeakResult('UNCERTAIN', 'NETWORK_ERROR')


def test_real_child_timeout_snapshot_is_alive_and_sanitized(caplog):
    client = client_module.ThingSpeakClient(2, sleeping_request)
    started = time.monotonic()
    result = client.publish(SECRET, {'field1': PAYLOAD_MARK})
    assert result.code == 'TIMEOUT' and time.monotonic() - started < 10
    timeout = observed(client)['timeout']
    assert set(timeout) == TIMEOUT_KEYS
    assert timeout['childAlive'] is True and timeout['exitCode'] is None
    assert timeout['lastMarker'] in (None, 'CHILD_ENTERED')
    assert SECRET not in json.dumps(observed(client)) + caplog.text
    assert PAYLOAD_MARK not in json.dumps(observed(client)) + caplog.text
