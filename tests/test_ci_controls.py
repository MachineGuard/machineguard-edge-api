import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('security_check',ROOT/'scripts/security-check.py')
security=importlib.util.module_from_spec(spec);spec.loader.exec_module(security)


def test_secret_gate_fails_without_printing_the_match(tmp_path):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    secret='ghp_'+'A'*40
    (tmp_path/'example.py').write_text('value="'+secret+'"\n')
    report=tmp_path/'report.json'
    run=subprocess.run([sys.executable,str(ROOT/'scripts/security-check.py'),'secrets','--report',str(report)],cwd=tmp_path,capture_output=True,text=True)
    assert run.returncode==1 and secret not in run.stdout+run.stderr+report.read_text()
    assert json.loads(report.read_text())['findings'][0]['kind']=='github-token'


def test_dependency_scan_keeps_advisories_and_fails_closed(monkeypatch):
    import io
    from urllib.error import URLError
    sent=[]
    def query(request,**kwargs):
        sent.append(json.loads(request.data))
        return io.BytesIO(b'{"results":[{"vulns":[{"id":"TEST-ADVISORY"}]}]}')
    monkeypatch.setattr(security.urllib.request,'urlopen',query)
    found=security.dependency_findings([('PyPI','example','1.0')])
    assert found[0]['advisories']==['TEST-ADVISORY']
    assert sent==[{'queries':[{'package':{'ecosystem':'PyPI','name':'example'},'version':'1.0'}]}]


def test_dependency_inputs_include_transitive_packages(tmp_path):
    lock=tmp_path/'requirements.txt';lock.write_text('example==1.0 \\\n    --hash=sha256:fake\ntransitive==2.0\n')
    assert security.packages(SimpleNamespace(requirements=str(lock)))==[('PyPI','example','1.0'),('PyPI','transitive','2.0')]
    tree=tmp_path/'tree.json';tree.write_text(json.dumps({'groupId':'local','artifactId':'root','version':'1',
        'children':[{'groupId':'group','artifactId':'lib','version':'2','children':[{'groupId':'other','artifactId':'transitive','version':'3'}]}]}))
    assert security.packages(SimpleNamespace(requirements=None,maven_json=str(tree)))==[('Maven','group:lib','2'),('Maven','other:transitive','3')]
