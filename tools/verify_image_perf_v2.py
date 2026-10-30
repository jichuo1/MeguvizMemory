#!/usr/bin/env python3
"""Qualify the image total-only harness against an independent scalar oracle.

No device calls or performance inference. Verification captures write after the
task, are explicitly diagnostic, and never mix with formal total-only records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from verify_image_workspace_v2 import model, oracle, rgb_fixture


ROOT = Path(__file__).resolve().parents[1]
DIMENSIONS = [(1,1),(1,2),(1,7),(1,15),(2,1),(2,2),(2,7),(7,1),(7,2),(17,13),(320,180),(1920,1080)]
VARIANTS = [(p,l) for p in ('direct','system','arena') for l in ('full','ring')]
MASK = (1 << 64)-1


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output_hash(pgm, frames):
    pixels=pgm.split(b'\n',3)[3]
    frame_hash=14695981039346656037
    for byte in pixels:
        frame_hash=((frame_hash ^ byte)*1099511628211)&MASK
    batch=14695981039346656037
    for _ in range(frames):
        for offset in range(8):
            batch=((batch ^ ((frame_hash >> (8*offset)) & 255))*1099511628211)&MASK
    return batch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--runtime-dir',type=Path)
    parser.add_argument('--out-dir',type=Path,default=ROOT/'build/image-perf-v2-checks')
    parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args()
    executable=args.exe.resolve()
    assert executable.is_file()
    args.out_dir.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ)
    if args.runtime_dir:
        env['PATH']=str(args.runtime_dir.resolve())+os.pathsep+env.get('PATH','')
    rows=[]
    fixtures=[]

    def run(label, source, provider, layout, extra=(), pgm=None, expected_sum=None, width=None, height=None, error=None):
        command=[str(executable),str(source),'--provider',provider,'--layout',layout,'--frames','3',*map(str,extra)]
        result=subprocess.run(command,capture_output=True,text=True,env=env,timeout=30)
        native=json.loads(result.stdout)
        checks={'stdout_is_single_json':True,'stderr_empty':not result.stderr.strip(),
                'source_final_released':native['source_final_released'] is True,
                'lease_final_correct':native['lease_final_correct'] is True,
                'closed_zero':native['closed_backing_bytes']==0 and native['final_resource_state']=='CLOSED_ZERO',
                'no_phase_clocks':native['phases'] is None}
        if error:
            checks.update({'expected_failure':result.returncode==1 and native['success'] is False,
                           'failure_reason':error in native['failure_reason']})
        else:
            expected=model(width,height,layout,provider)
            capture='--verify-output' in extra
            checks.update({'complete_success':result.returncode==0 and native['success'] is True,
                           'input_sha256':native['source_sha256']==sha(source),
                           'input_bytes':native['input_bytes']==source.stat().st_size,
                           'dimensions':native['width']==width and native['height']==height,
                           'complete_batch':native['frames']==native['completed_frames']==3,
                           'one_conversion_per_pixel':native['gray_conversions']==width*height*3,
                           'all_pixel_sum':native['result_checksum']==expected_sum*3,
                           'complete_output_hash':native['output_hash_fnv64']==output_hash(pgm,3),
                           'diagnostic_flag':native['diagnostic'] is capture,
                           'positive_total':type(native['total_ns']) is int and native['total_ns']>0,
                           'backing_model':native['unique_owned_backing_peak_bytes']==expected['peak_unique_owned_backing_bytes'],
                           'payload_model':native['logical_requested_bytes']==expected['logical_payload_bytes'],
                           'provider_model':native['provider_allocation_count']==expected['provider_allocation_count'],
                           'actual_steady_state_calls':native['steady_state_provider_allocations']==0,
                           'unknowns_remain_null':all(native[k] is None for k in ('arena_used_bytes','input_backing_padding_bytes','memory_metadata_bytes','rss_pss_bytes','energy'))})
            if capture:
                target=Path(extra[extra.index('--verify-output')+1])
                checks['byte_exact_output']=target.read_bytes()==pgm
        rows.append({'case':label,'provider':provider,'layout':layout,'exit_code':result.returncode,
                     'native':native,'checks':checks,'status':'PASS' if all(checks.values()) else 'FAIL'})
        assert all(checks.values()),(label,checks,native)

    for width,height in DIMENSIONS:
        name=f'{width}x{height}'
        source=args.out_dir/f'{name}.ppm'
        rgb=rgb_fixture(width,height)
        source.write_bytes(f'P6\n{width} {height}\n255\n'.encode()+rgb)
        pgm,total=oracle(width,height,rgb)
        fixtures.append({'name':name,'input_bytes':source.stat().st_size,'input_sha256':sha(source),
                         'width':width,'height':height,'frames':3,'result_checksum':total*3,
                         'output_hash_fnv64':output_hash(pgm,3),'oracle_pgm_sha256':hashlib.sha256(pgm).hexdigest()})
        for provider,layout in VARIANTS:
            capture=args.out_dir/f'{name}-{provider}-{layout}.pgm'
            run(f'{name}-{provider}-{layout}-capture',source,provider,layout,
                ['--verify-output',capture],pgm,total,width,height)
            run(f'{name}-{provider}-{layout}-formal',source,provider,layout,[],pgm,total,width,height)
    source=args.out_dir/'17x13.ppm'
    bad=args.out_dir/'truncated.ppm'
    bad.write_bytes(b'P6\n2 2\n255\n\0')
    output_directory=args.out_dir/'output-is-directory'
    output_directory.mkdir(exist_ok=True)
    for provider,layout in VARIANTS:
        run(f'worker-failure-{provider}-{layout}',source,provider,layout,['--fault-worker'],error='injected worker exception')
        run(f'truncated-{provider}-{layout}',bad,provider,layout,error='truncated input')
        run(f'output-open-failure-{provider}-{layout}',source,provider,layout,['--verify-output',output_directory],error='cannot open verification output')
    receipt={'schema_version':2,'scope':'HOST_IMAGE_PERF_FUNCTIONAL_ONLY_NO_PERFORMANCE_INFERENCE',
             'status':'PASS','device_executed':False,'performance_qualified':False,
             'executable':{'path':executable.relative_to(ROOT).as_posix(),'sha256':sha(executable)},
             'native_source_sha256':sha(ROOT/'consumers/image/perf.cpp'),
             'cmake_source_sha256':sha(ROOT/'consumers/image/CMakeLists.txt'),
             'verifier_sha256':sha(Path(__file__)),'oracle_source_sha256':sha(ROOT/'tools/verify_image_workspace_v2.py'),
             'cases':len(rows),'fixtures':fixtures,'runs':rows,
             'timing_contract':'Formal excludes preload/hash/loader/file output; includes workspace, worker creation/join, input copies, integer gray+blur, complete output digest, cleanup and common source final release.',
             'capture_contract':'--verify-output marks diagnostic=true; last output copied during task solely for byte oracle and file written after timed cleanup; never pooled with formal.',
             'attribution':'I-DR/I-DF ordinary layout; I-SR/I-DR Memory interface; I-AR/I-SR arena; I-AR/I-DR Memory net versus persistent direct ring. No pooling between contrasts.',
             'limitations':['Synthetic deterministic image is a real image algorithm workload, not a BIL production trace.',
                            'Input copying and worker lifetime are a fixed controlled contract; resource reductions do not establish fastest conceivable A1.',
                            'Requested backing and source capacity are not RSS/PSS, metadata or energy.',
                            'Host elapsed values are functional records only; no local speedup inference.']}
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    args.receipt.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'status':'PASS','cases':len(rows),'receipt':str(args.receipt)}))


if __name__=='__main__':
    main()
