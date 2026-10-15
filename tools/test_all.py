"""Run each unittest module in an isolated process and write a truthful report."""
from concurrent.futures import ThreadPoolExecutor
import argparse,json,re,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--report',type=Path,default=ROOT/'docs/verification/tests');args=p.parse_args()
args.report.mkdir(parents=True,exist_ok=True)
def run(path):
    try:
        result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p',path.name,'-v'],
            cwd=ROOT,capture_output=True,text=True,timeout=60)
        text=result.stdout+result.stderr
        status='passed' if result.returncode==0 else 'failed'
    except subprocess.TimeoutExpired as exc:
        text=(exc.stdout or b'').decode(errors='replace')+(exc.stderr or b'').decode(errors='replace')
        text+='\nTIMEOUT after 60 seconds. Not a successful test run.\n';status='timeout'
    (args.report/(path.stem+'.log')).write_text(text)
    count=re.search(r'Ran (\d+) tests?',text)
    skipped=re.search(r'skipped=(\d+)',text)
    return dict(module=path.name,status=status,tests=int(count[1]) if count else 0,skipped=int(skipped[1]) if skipped else 0)
with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(run,sorted((ROOT/'tests').glob('test_*.py'))))
(args.report/'results.json').write_text(json.dumps(results,indent=2)+'\n')
for item in results:print(item)
print('Executed:',sum(x['tests']-x['skipped'] for x in results),'Skipped:',sum(x['skipped'] for x in results))
raise SystemExit(0 if all(x['status']=='passed' for x in results) else 1)
