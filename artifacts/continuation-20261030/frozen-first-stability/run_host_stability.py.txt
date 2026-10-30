"""Finite concurrent native consumer stability; never a timing comparison."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import time
from run_continuation_device import native_checks, stability_plan, ROOT, digest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir',type=Path,required=True)
    parser.add_argument('--min-seconds',type=int,default=900)
    parser.add_argument('--wall-seconds',type=int,default=1200)
    args=parser.parse_args();out=args.out_dir.resolve()
    if not out.is_relative_to(ROOT/'artifacts/continuation-20261030') or out.exists():parser.error('new continuation directory required')
    if not 0<=args.min_seconds<=900 or not args.min_seconds<args.wall_seconds<=1200:parser.error('invalid duration bounds')
    out.mkdir(parents=True);(out/'commands').mkdir()
    runtime=ROOT/'install/features-host/bin';env=dict(os.environ);env['PATH']=str(runtime)+os.pathsep+env.get('PATH','')
    dex=ROOT/'build/continuation-20261030/dex-host/mgmem_dex_import_consumer.exe'
    image=ROOT/'build/continuation-image-host/mgmem_image_pressure_stability.exe'
    fixtures=ROOT/'artifacts/continuation-20261030/image-host'
    jobs={'dex':[str(dex),str(ROOT/'artifacts/local/perf-inputs/bilibili-9.17.0-classes.dex'),'--variant','import','--case','cross-thread','--cycles','4','--min-duration-ms',str(args.min_seconds*1000),'--wall-ms',str(args.wall_seconds*1000)]}
    for provider in ('system','arena'):
        dest=ROOT/'build'/('continuation-host-output-'+provider);dest.mkdir(exist_ok=True)
        command=[str(image),'--provider',provider,'--cycles','4','--hold-ms','0','--min-duration-ms',str(args.min_seconds*1000),'--wall-ms',str(args.wall_seconds*1000),'--out-dir',str(dest)]
        for index in range(4):command+=['--input',str(fixtures/f'input-{index}.ppm'),'--oracle',str(fixtures/f'oracle-{index}.pgm')]
        jobs['image-'+provider]=command
    inputs=[Path(__file__),ROOT/'tools/run_continuation_device.py',ROOT/'consumers/dex/import_consumer.cpp',ROOT/'consumers/image/pressure_stability.cpp']
    receipt={'state':'RUNNING','scope':'HOST_CONCURRENT_BOUNDED_STABILITY_NOT_PERFORMANCE','minimum_seconds_per_workload':args.min_seconds,'wall_seconds_per_workload':args.wall_seconds,'source_sha256':digest(Path(__file__)),'source_inputs':{p.relative_to(ROOT).as_posix():digest(p) for p in inputs},'binaries':{p.name:digest(p) for p in (dex,image,runtime/'mgmem.dll')},'mixed_workloads':list(jobs),'results':{},'raw_outputs':'commands/ locally retained; public checked summaries and hashes','BIL':'NOT_MODIFIED_NOT_INTEGRATED'}
    def save():(out/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if args.min_seconds:receipt['stability_plan_sha256']=stability_plan(args.min_seconds,args.wall_seconds)
    save()
    def execute(name,command):
        start=time.monotonic()
        try:
            result=subprocess.run(command,env=env,capture_output=True,timeout=args.wall_seconds+60)
            stdout,stderr=result.stdout,result.stderr;code=result.returncode;timed_out=False
        except subprocess.TimeoutExpired as error:
            stdout,stderr=error.stdout or b'',error.stderr or b'';code=None;timed_out=True
        for label,data in [('stdout',stdout),('stderr',stderr)]:(out/'commands'/(name+'.'+label+'.txt')).write_bytes(data)
        info={'exit_code':code,'timed_out':timed_out,'host_elapsed_seconds':time.monotonic()-start,'stdout_sha256':digest(out/'commands'/(name+'.stdout.txt')),'stderr_sha256':digest(out/'commands'/(name+'.stderr.txt'))}
        try:
            if code!=0 or timed_out or stderr.strip():raise RuntimeError('command_failed_or_incomplete')
            info['checked']=native_checks('dex' if name=='dex' else 'image',json.loads(stdout),args.min_seconds)
            if name.startswith('image'):
                provider=name.split('-')[1];dest=ROOT/'build'/('continuation-host-output-'+provider)
                for index in range(4):
                    if digest(dest/f'frame-{index}.pgm')!=digest(fixtures/f'oracle-{index}.pgm'):raise RuntimeError('final_output_oracle')
            info['state']='PASS'
        except Exception as error:info.update(state='FAIL_OR_INCOMPLETE',reason=str(error))
        return name,info
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(execute,name,command) for name,command in jobs.items()]):
            name,info=future.result();receipt['results'][name]=info;save();print(json.dumps({'workload':name,'state':info['state']}),flush=True)
    receipt['state']='PASS' if all(v['state']=='PASS' for v in receipt['results'].values()) else 'FAIL_OR_INCOMPLETE';save()
    raise SystemExit(0 if receipt['state']=='PASS' else 2)

if __name__=='__main__':main()
