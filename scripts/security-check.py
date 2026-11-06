"""Small CI gates: tracked-file secret patterns and resolved-package OSV queries."""
import argparse,json,re,subprocess,sys,urllib.request
from pathlib import Path


def secret_findings(root):
    names=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=root).decode().split('\0')
    patterns={'private-key':r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
        'github-token':r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b',
        'aws-access-key':r'\bAKIA[A-Z0-9]{16}\b',
        'jwt':r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}'}
    findings=[]
    for name in sorted(set(names)):
        path=root/name
        if not name or not path.is_file():continue
        try:text=path.read_text(encoding='utf-8')
        except UnicodeError:continue
        for number,line in enumerate(text.splitlines(),1):
            for kind,pattern in patterns.items():
                if re.search(pattern,line):findings.append({'path':name,'line':number,'kind':kind})
            if path.name.startswith('.env') and re.match(r'\s*(?:EDGE_ADMIN_KEY|EDGE_DEVICE_CREDENTIAL|THINGSPEAK_WRITE_KEY)\s*=\s*\S',line):
                findings.append({'path':name,'line':number,'kind':'configured-secret'})
    return findings


def packages(args):
    if args.requirements:
        return sorted({('PyPI',m[1],m[2]) for line in Path(args.requirements).read_text().splitlines()
            if (m:=re.match(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)',line))})
    root=json.loads(Path(args.maven_json).read_text());found=set()
    def visit(node,top=False):
        if not top:found.add(('Maven',node['groupId']+':'+node['artifactId'],node['version']))
        for child in node.get('children',[]):visit(child)
    visit(root,True)
    return sorted(found)


def dependency_findings(items):
    findings=[]
    for start in range(0,len(items),100):
        batch=items[start:start+100]
        request=urllib.request.Request('https://api.osv.dev/v1/querybatch',
            data=json.dumps({'queries':[{'package':{'ecosystem':ecosystem,'name':name},'version':version}
                for ecosystem,name,version in batch]}).encode(),headers={'Content-Type':'application/json'},method='POST')
        with urllib.request.urlopen(request,timeout=30) as response:data=json.load(response)
        results=data['results']
        if len(results)!=len(batch):raise ValueError('Incomplete advisory response')
        for package,result in zip(batch,results):
            if result.get('next_page_token'):raise ValueError('Incomplete paginated advisory response')
            if result.get('vulns'):findings.append({'package':package,'advisories':sorted({v['id'] for v in result['vulns']})})
    return findings


def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['secrets','dependencies'])
    parser.add_argument('--requirements');parser.add_argument('--maven-json');parser.add_argument('--report',required=True)
    args=parser.parse_args();report=Path(args.report);report.parent.mkdir(parents=True,exist_ok=True)
    try:
        if args.mode=='secrets':result={'scope':'tracked and unignored working files; patterns, not full-history scan','findings':secret_findings(Path.cwd())}
        else:
            if bool(args.requirements)==bool(args.maven_json):raise ValueError('Choose one resolved package input')
            items=packages(args)
            if not items:raise ValueError('Empty resolved package input')
            result={'source':'https://api.osv.dev/v1/querybatch','packages':len(items),'findings':dependency_findings(items)}
        report.write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(args.mode,'findings:',len(result['findings']))
        return int(bool(result['findings']))
    except Exception:
        report.write_text(json.dumps({'status':'NOT_COMPLETED','reason':'Scanner input or advisory service unavailable'}),encoding='utf-8')
        print('Security gate not completed; inspect configuration/connectivity, no clean result claimed')
        return 2


if __name__=='__main__':sys.exit(main())
