"""Windows child bootstrap must not create a second application/worker runtime."""
import runpy
from pathlib import Path


def test_development_entrypoint_has_no_runtime_side_effect_in_spawn_child(monkeypatch):
    import edge_api
    calls=[]
    monkeypatch.setattr(edge_api,'create_app',lambda:calls.append('unexpected-child-runtime'))
    runpy.run_path(str(Path(__file__).resolve().parents[1]/'run.py'),run_name='__mp_main__')
    assert calls==[]
