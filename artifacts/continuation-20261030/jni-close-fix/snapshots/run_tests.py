from pathlib import Path
import hashlib
import json
import os
import subprocess

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
env=dict(os.environ)
env['PATH']=str(ROOT/'install/features-host/bin')+os.pathsep+env.get('PATH','')
cases=('callback-reentry','single-publisher','attach-retry','java-exception-retry','error-quarantine',
       'foreign-release','foreign-release-bind','foreign-release-snapshot')
rows=[]
for mode in ('old','new'):
    for case in cases:
        phase=OUT/f'phase-{mode}-{case}.txt'
        case_env=dict(env);case_env['JNI_REVIEW_PHASE_FILE']=str(phase)
        try:
            result=subprocess.run([str(OUT/f'out/jni_lock_{mode}.exe'),case],capture_output=True,text=True,env=case_env,timeout=3)
            row={'implementation':mode,'case':case,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'timed_out':False}
        except subprocess.TimeoutExpired as error:
            row={'implementation':mode,'case':case,'exit_code':None,'stdout':(error.stdout or b'').decode() if isinstance(error.stdout,bytes) else (error.stdout or ''),
                 'stderr':(error.stderr or b'').decode() if isinstance(error.stderr,bytes) else (error.stderr or ''),'timed_out':True}
        row['phase']=phase.read_text(encoding='utf-8') if phase.exists() else None
        try:row['native']=json.loads(row['stdout'])
        except json.JSONDecodeError:row['native']=None
        rows.append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def correct(row):
    if row['implementation']=='old' and row['case']=='foreign-release-bind':
        return row['timed_out'] and row['phase']=='FOREIGN_RELEASE_ENTER_nativeBind\n'
    return not row['timed_out'] and row['exit_code']==(1 if row['implementation']=='old' else 0) and not row['stderr']
receipt={'schema_version':1,'state':'PASS_COUNTEREXAMPLES_AND_CANDIDATE' if all(correct(row) for row in rows) else 'FAIL',
         'scope':'HOST_FAKE_JNI_ACTUAL_CALLBACK_REENTRY_AND_REAL_PRESSURE_CONTROLLER',
         'production_source_sha256':sha(ROOT/'adapters/android/native/pressure_jni.cpp'),
         'files':[{'path':p.name,'sha256':sha(p)} for p in (OUT/'pressure_jni.original.cpp',OUT/'pressure_jni.candidate.cpp',OUT/'lock-boundary-candidate.patch',OUT/'fake_jni_test.cpp',OUT/'CMakeLists.txt',OUT/'run_tests.py')],
         'binaries':[{'name':f'jni_lock_{mode}.exe','sha256':sha(OUT/f'out/jni_lock_{mode}.exe')} for mode in ('old','new')],
         'runtime':{'name':'mgmem.dll','sha256':sha(ROOT/'install/features-host/bin/mgmem.dll')},
         'runs':rows,'production_modified':False,'Android_executed':False,
         'limits':['Fake JNI function tables execute the actual JNI composition C++ and real host pressure worker; no real JVM/ART or Android runtime.',
                   'Both source copies receive the same host-JDK AttachCurrentThread void** signature compatibility edit only.',
                   'Constructed callback/release reentry demonstrates a lock boundary defect, not the unproven cause of the real Android hang.',
                   'Error-quarantine deliberately retains registration and fake pins, not a resource-zero claim.']}
(OUT/'test-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'state':receipt['state']}))
if receipt['state'].startswith('FAIL'):raise SystemExit(1)
