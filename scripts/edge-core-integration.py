"""CI/local contract with explicit checkouts, fresh loopback PostgreSQL, real IAM and Core.

Requires a Python venv installed from Edge's requirements-test.txt with --require-hashes.
DB_URL and MACHINEGUARD_TEST_DB_URL must point to separate, disposable mgci databases.
"""
import argparse,importlib.util,json,os,re,secrets,shutil,socket,subprocess,sys,tempfile,time,traceback
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from output_sanitizer import sanitize_text,redirect_report_path,publish_artifacts

import requests


def free_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def revision(path,supplied):
    if supplied:
        if not re.fullmatch('[0-9a-f]{40}',supplied):raise ValueError('Require immutable SHA')
        return supplied
    return subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip()


class Run:
    def __init__(self,args):
        self.args=args;self.core=args.core.resolve();self.edge=args.edge.resolve()
        self.report=args.report.resolve();self.report.mkdir(parents=True,exist_ok=True)
        self.runtime=Path(tempfile.mkdtemp(prefix='machineguard-ci-'))
        self.private=[secrets.token_urlsafe(40) for _ in range(3)]
        self.admin_password,self.jwt,self.edge_key=self.private
        self.env=dict(os.environ)
        for key in list(self.env):
            if key.startswith(('EDGE_','THINGSPEAK_','IAM_','MACHINEGUARD_CORE_')):self.env.pop(key)
        for name in ('DB_URL','MACHINEGUARD_TEST_DB_URL'):
            if not re.fullmatch(r'jdbc:postgresql://127\.0\.0\.1:[0-9]+/mgci(?:_test)?',self.env.get(name,'')):
                raise ValueError('Require fresh loopback mgci/mgci_test databases')
        if self.env['DB_URL']==self.env['MACHINEGUARD_TEST_DB_URL']:raise ValueError('Separate runtime/test databases')
        self.result={'coreSha':revision(self.core,args.core_sha),'edgeSha':revision(self.edge,args.edge_sha),
            'python':sys.version,'runtime':str(self.runtime),'commands':[],
            'postgresUrl':self.env['DB_URL'],'testPostgresUrl':self.env['MACHINEGUARD_TEST_DB_URL']}
        self.base=f'http://127.0.0.1:{free_port()}';self.process=None;self.core_log=None;self.starts=0

    def clean(self,text):
        return sanitize_text(text,self.private)

    def command(self,label,argv,cwd,env=None,timeout=1200):
        start=time.monotonic()
        with tempfile.TemporaryDirectory(prefix='private-reports-',dir=self.runtime) as temporary:
            staging=Path(temporary)
            assert staging.resolve().is_relative_to(self.runtime.resolve())
            args=[redirect_report_path(value,self.report,staging) for value in argv]
            command_env={key:redirect_report_path(value,self.report,staging) for key,value in (env or self.env).items()}
            try:
                p=subprocess.run(args,cwd=cwd,env=command_env,capture_output=True,text=True,
                    encoding='utf-8',errors='replace',timeout=timeout)
                code=p.returncode;output=p.stdout+p.stderr
            except subprocess.TimeoutExpired as error:
                def decode(value):return value.decode('utf-8',errors='replace') if isinstance(value,bytes) else value or ''
                code=124;output=decode(error.stdout)+decode(error.stderr)+'\nCommand timed out\n'
            except OSError as error:
                code=127;output='Command could not start: '+str(error)
            publish_artifacts(staging,self.report,self.clean)
        (self.report/(label+'.log')).write_text(self.clean(output),encoding='utf-8')
        self.result['commands'].append({'label':label,'argv':list(map(str,argv)),'cwd':str(cwd),
            'exitCode':code,'seconds':round(time.monotonic()-start,3)})
        self.save();print(self.clean(label),'exit',code,flush=True)
        if code:raise RuntimeError(label+' failed; inspect sanitized report')

    def save(self):
        (self.report/'results.json').write_text(self.clean(json.dumps(self.result,indent=2)),encoding='utf-8')

    def start_core(self):
        env=dict(self.env,SERVER_ADDRESS='127.0.0.1',SERVER_PORT=self.base.rsplit(':',1)[1],
            IAM_JWT_SECRET=self.jwt,IAM_BOOTSTRAP_ENABLED='true',IAM_BOOTSTRAP_ORGANIZATION_NAME='CI synthetic',
            IAM_BOOTSTRAP_ADMIN_NAME='CI synthetic admin',IAM_BOOTSTRAP_ADMIN_EMAIL='admin@ci.machineguard.invalid',
            IAM_BOOTSTRAP_ADMIN_PASSWORD=self.admin_password,CORE_OPENAPI_ENABLED='false',IAM_LOGIN_LIMIT='100')
        env.pop('JAVA_TOOL_OPTIONS',None)
        self.core_log=tempfile.TemporaryFile();self.starts+=1
        jar=next(self.core.glob('target/core-api-*.jar'))
        self.process=subprocess.Popen(['java','-jar',str(jar)],cwd=self.core,env=env,
            stdout=self.core_log,stderr=subprocess.STDOUT,**({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        for _ in range(200):
            if self.process.poll() is not None:raise RuntimeError('Owned Core exited during startup')
            try:
                if requests.get(self.base+'/api/v1/environmental-monitoring/zones',timeout=1).status_code==401:return
            except requests.RequestException:pass
            time.sleep(.25)
        raise RuntimeError('Owned Core readiness timeout')

    def stop_core(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=20)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
            self.core_log.seek(0)
            (self.report/f'core-runtime-{self.starts}.log').write_text(self.clean(self.core_log.read().decode(errors='replace')),encoding='utf-8')
            self.core_log.close();self.process=None

    def execute(self):
        maven=self.args.maven
        if self.args.contracts_only:
            self.result['checksSkipped']='Core suites: focused development repetition only'
            return self.contracts()
        unit_env=dict(self.env)
        for key in ('DB_URL','DB_USERNAME','DB_PASSWORD','MACHINEGUARD_TEST_DB_URL','JAVA_TOOL_OPTIONS'):unit_env.pop(key,None)
        reports=['-Dmachineguard.test.reports.directory='+str(self.report/'core-unit-surefire'),
            '-Dcucumber.plugin=pretty,summary,junit:'+str(self.report/'core-unit-cucumber/cucumber.xml')]
        self.command('core-verify',[maven,'-B',*reports,'verify'],self.core,unit_env)
        self.result['coreUnitTests']=sum(int(ET.parse(p).getroot().get('tests','0')) for p in (self.report/'core-unit-surefire').glob('TEST-*.xml'))
        subset='GatewayCredentialApiIntegrationTest,GatewayIngestionApiIntegrationTest,AlertIncidentFlowIntegrationTest,MeasurementTimeApiIntegrationTest,WriteSessionApiIntegrationTest,WriteRouteCoverageIntegrationTest,LoginRateLimitApiIntegrationTest'
        test_env=dict(self.env,JAVA_TOOL_OPTIONS='-Dspring.datasource.driver-class-name=org.postgresql.Driver '
            '-Dspring.datasource.username='+self.env.get('DB_USERNAME','mgci')+' -Dspring.datasource.password=')
        self.command('core-postgres',[maven,'-B','-Dtest='+subset,
            '-Dmachineguard.test.reports.directory='+str(self.report/'core-postgres-surefire'),'test'],self.core,test_env)
        self.result['postgresSubset']='Repeated subset; not added to Core unit suite total'
        self.contracts()

    def contracts(self):
        try:
            self.start_core()
            env=dict(self.env,MACHINEGUARD_CORE_URL=self.base,MACHINEGUARD_CORE_ADMIN_EMAIL='admin@ci.machineguard.invalid',
                MACHINEGUARD_CORE_ADMIN_PASSWORD=self.admin_password)
            junit=self.report/'edge-junit.xml'
            self.command('edge-full-live',[sys.executable,'-m','pytest','-q','--tb=short','-p','no:cacheprovider',
                '--junitxml='+str(junit)],self.edge,env)
            cases=ET.parse(junit).getroot().findall('.//testcase')
            live=[c for c in cases if c.get('classname','').endswith('test_live_core')]
            if len(live)<5 or any(c.find('skipped') is not None for c in cases):raise RuntimeError('Live integration omitted tests')
            self.result['edgeTests']=len(cases);self.result['liveTests']=len(live)
            sys.path.insert(0,str(self.edge))
            spec=importlib.util.spec_from_file_location('ci_probe',self.edge/'tests/ci_probe.py')
            probe=importlib.util.module_from_spec(spec);sys.modules[spec.name]=probe;spec.loader.exec_module(probe)
            self.result['secondaryContract']=probe.execute(self)
            self.result['completed']=True
        finally:
            self.stop_core();self.result['ownedCoreStopped']=self.process is None;self.save()


def main():
    p=argparse.ArgumentParser();p.add_argument('--core',type=Path,required=True)
    p.add_argument('--edge',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    p.add_argument('--maven',default='mvn.cmd' if os.name=='nt' else 'mvn')
    p.add_argument('--core-sha');p.add_argument('--edge-sha');p.add_argument('--contracts-only',action='store_true');args=p.parse_args()
    run=Run(args)
    try:
        try:run.execute()
        finally:
            try:run.stop_core()
            finally:run.save()
    except Exception:
        print(run.clean(traceback.format_exc()),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':sys.exit(main())
