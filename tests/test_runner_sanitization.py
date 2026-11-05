"""Exercise actual runner failure/publication paths using disposable synthetic values."""
import base64
import importlib.util
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def runner(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    spec=importlib.util.spec_from_file_location('integration_runner',ROOT/'scripts/edge-core-integration.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setenv('DB_URL','jdbc:postgresql://127.0.0.1:19991/mgci')
    monkeypatch.setenv('MACHINEGUARD_TEST_DB_URL','jdbc:postgresql://127.0.0.1:19991/mgci_test')
    args=SimpleNamespace(core=tmp_path,edge=ROOT,report=tmp_path/'published',
                         core_sha='1'*40,edge_sha='2'*40)
    run=module.Run(args)
    # A fixed UUID and 48-byte, URL-safe, unpadded token match Core's emitted format.
    suffix=base64.urlsafe_b64encode(bytes(range(48))).decode().rstrip('=')
    gateway='mgg_'+'01234567-89ab-cdef-0123-456789abcdef'+'.'+suffix
    node='mgn_'+base64.urlsafe_b64encode(b'synthetic-node-only'*2).decode().rstrip('=')
    encode=lambda data:base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    jwt=encode({'alg':'HS256','typ':'JWT'})+'.'+encode({'sub':'synthetic-only'})+'.'+encode({'signature':'not-a-real-signature'})
    values=[gateway,node,jwt,*run.private]
    return run,module,values,suffix


@pytest.mark.parametrize('wrap',[lambda value:value,lambda value:'X-Device-Credential: '+value,
    lambda value:json.dumps({'message':value}),lambda value:'<failure message="'+value+'"/>'])
def test_emitted_gateway_suffix_and_other_secrets_are_removed(runner,wrap):
    run,module,values,suffix=runner
    clean=run.clean('useful-diagnostic '+wrap(' | '.join(values)))
    assert all(value not in clean for value in values), 'Credential survived output sanitation'
    assert suffix not in clean, 'Gateway suffix survived partial redaction'
    assert 'useful-diagnostic' in clean
    assert run.clean('healthy 6 measurements / 1 alert')=='healthy 6 measurements / 1 alert'


PRODUCER='''
import json,os,sys
from pathlib import Path
import xml.etree.ElementTree as ET
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
text='retained-diagnostic '+os.environ['SYNTHETIC_VALUES']
print(text,flush=True);print(text,file=sys.stderr,flush=True)
(out/'failure.json').write_text(json.dumps({'diagnostic':text}),encoding='utf-8')
ET.ElementTree(ET.Element('failure',message=text)).write(out/'failure.xml',encoding='utf-8')
sys.exit(7)
'''


def test_failed_subprocess_retains_only_sanitized_reports_and_diagnostic(runner,tmp_path,capsys,monkeypatch):
    run,module,values,suffix=runner
    producer=tmp_path/'producer.py';producer.write_text(PRODUCER,encoding='utf-8')
    env=dict(os.environ,SYNTHETIC_VALUES=' | '.join(values))
    # Every parent-side write into the public report must already be clean.
    original_write=Path.write_text
    def checked_write(path,text,*args,**kwargs):
        if path.is_relative_to(run.report):
            assert all(value not in text for value in values), 'Unsafe write to public report'
        return original_write(path,text,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',checked_write)
    with pytest.raises(RuntimeError,match='failed'):
        run.command('intentional-failure',[sys.executable,str(producer),str(run.report)],tmp_path,env)
    public='\n'.join(path.read_text(encoding='utf-8') for path in run.report.rglob('*') if path.is_file())
    visible=capsys.readouterr()
    assert all(value not in public+visible.out+visible.err for value in values)
    assert suffix not in public+visible.out+visible.err
    assert 'retained-diagnostic' in (run.report/'intentional-failure.log').read_text()
    assert json.loads((run.report/'failure.json').read_text())['diagnostic'].startswith('retained-diagnostic')
    assert ET.parse(run.report/'failure.xml').getroot().get('message').startswith('retained-diagnostic')
    assert json.loads((run.report/'results.json').read_text())['commands'][0]['exitCode']==7


def test_timeout_keeps_partial_output_and_artifacts_sanitized(runner,tmp_path,capsys):
    run,module,values,suffix=runner
    producer=tmp_path/'slow.py'
    producer.write_text(PRODUCER.replace('sys.exit(7)','import time; time.sleep(5)'),encoding='utf-8')
    with pytest.raises(RuntimeError,match='failed'):
        run.command('timeout',[sys.executable,str(producer),str(run.report)],tmp_path,
                    dict(os.environ,SYNTHETIC_VALUES=' | '.join(values)),timeout=1)
    public='\n'.join(path.read_text() for path in run.report.rglob('*') if path.is_file())
    assert all(value not in public for value in values) and suffix not in public
    assert 'retained-diagnostic' in public and 'timed out' in public
    assert run.result['commands'][0]['exitCode']==124


def test_cleanup_exception_cannot_publish_secret_or_skip_result_save(runner,monkeypatch,capsys):
    run,module,values,suffix=runner
    def fail():raise RuntimeError('retained-cleanup-diagnostic '+values[0]+' '+values[-1])
    run.execute=fail;run.stop_core=fail
    run.result['diagnostic']='retained-result-diagnostic '+values[0]
    monkeypatch.setattr(module,'Run',lambda _args:run)
    monkeypatch.setattr(sys,'argv',['runner','--core',str(run.core),'--edge',str(run.edge),'--report',str(run.report)])
    assert module.main()==1
    visible=capsys.readouterr();public=(run.report/'results.json').read_text()
    assert all(value not in visible.out+visible.err+public for value in values)
    assert suffix not in visible.err+public
    assert 'retained-cleanup-diagnostic' in visible.err and 'retained-result-diagnostic' in public


def test_core_log_export_and_save_use_same_boundary(runner,capsys):
    import tempfile
    run,module,values,suffix=runner
    run.process=SimpleNamespace(poll=lambda:0);run.starts=1
    run.core_log=tempfile.TemporaryFile()
    run.core_log.write(('core-diagnostic '+' | '.join(values)).encode());run.core_log.flush()
    run.result['diagnostic']=' | '.join(values)
    run.stop_core();run.save()
    result=(run.report/'results.json').read_text()+(run.report/'core-runtime-1.log').read_text()
    assert all(value not in result for value in values) and suffix not in result
    assert 'core-diagnostic' in result


def test_credentials_are_removed_inside_diagnostic_labels(runner):
    run,module,values,suffix=runner
    output=run.clean('credential_'+' / credential_'.join(values))
    assert all(value not in output for value in values) and suffix not in output


def test_maven_failure_keeps_surefire_and_cucumber_private_until_sanitized(runner,tmp_path,monkeypatch,capsys):
    run,module,values,suffix=runner
    producer=tmp_path/'fake_maven.py'
    producer.write_text('''
import json,os,sys
from pathlib import Path
import xml.etree.ElementTree as ET
properties=dict(arg[2:].split('=',1) for arg in sys.argv[1:] if arg.startswith('-D') and '=' in arg)
surefire=Path(properties.get('machineguard.test.reports.directory','target/surefire-reports'))
cucumber=Path(properties.get('cucumber.plugin','junit:target/cucumber-reports/cucumber.xml').split('junit:')[-1])
surefire.mkdir(parents=True,exist_ok=True);cucumber.parent.mkdir(parents=True,exist_ok=True)
text='maven-failure-diagnostic '+os.environ['SYNTHETIC_VALUES']
ET.ElementTree(ET.Element('failure',message=text)).write(surefire/'TEST-failure.xml',encoding='utf-8')
ET.ElementTree(ET.Element('failure',message=text)).write(cucumber,encoding='utf-8')
(surefire/'failure.json').write_text(json.dumps({'error':text}),encoding='utf-8')
print(text,flush=True);print(text,file=sys.stderr,flush=True)
sys.exit(7)
''',encoding='utf-8')
    real_command=run.command
    def fake_maven(label,argv,cwd,env=None):
        return real_command(label,[sys.executable,str(producer),*argv[1:]],cwd,env)
    monkeypatch.setattr(run,'command',fake_maven);run.args.maven='synthetic-maven'
    run.args.contracts_only=False;run.env['SYNTHETIC_VALUES']=' | '.join(values)
    with pytest.raises(RuntimeError,match='failed'):run.execute()
    files=[path for path in tmp_path.rglob('*') if path.suffix in ('.xml','.json','.log')]
    text='\n'.join(path.read_text() for path in files)
    assert all(value not in text for value in values), 'Maven report bypassed sanitation'
    assert suffix not in text and 'maven-failure-diagnostic' in text
    assert not (tmp_path/'target/surefire-reports').exists()
    assert ET.parse(run.report/'core-unit-surefire/TEST-failure.xml').getroot().get('message').startswith('maven-failure-diagnostic')
    assert ET.parse(run.report/'core-unit-cucumber/cucumber.xml').getroot().get('message').startswith('maven-failure-diagnostic')
