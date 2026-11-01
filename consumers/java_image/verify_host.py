#!/usr/bin/env python3
"""Run a bounded host image comparison, never an Android or performance test."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[2]


def sha(data):return hashlib.sha256(data).hexdigest()


def oracle(width,height,rgb):
    luma=[(77*rgb[3*i]+150*rgb[3*i+1]+29*rgb[3*i+2])>>8 for i in range(width*height)]
    out=bytearray()
    for y in range(height):
        for x in range(width):
            total=sum(luma[min(max(y+dy,0),height-1)*width+min(max(x+dx,0),width-1)]
                      for dy in (-1,0,1) for dx in (-1,0,1))
            out.append(total//9)
    return ('P5\n%d %d\n255\n'%(width,height)).encode('ascii')+bytes(out)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for key in ('java','javac','reference','core','jni','consumer-jni','classes','adapter-jar','out-dir'):
        ap.add_argument('--'+key,type=Path,required=True)
    ap.add_argument('--plain-classes',type=Path,help='Classes from a Java-only consumer build without the optional identity probe')
    a=ap.parse_args();out=a.out_dir.resolve()
    if not out.is_relative_to((ROOT/'build').resolve()) or out.exists():ap.error('fresh output directory under build/ required')
    inputs=[a.java,a.javac,a.reference,a.core,a.jni,a.consumer_jni,a.adapter_jar]
    if not all(p.is_file() for p in inputs) or not a.classes.is_dir():ap.error('existing installed/build inputs required')
    out.mkdir(parents=True);logs=out/'private-logs';logs.mkdir()
    source=ROOT/'adapters/android/java/io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.java'
    source_before=sha(source.read_bytes())
    managed_source=ROOT/'adapters/android/java/io/github/jichuo1/meguviz/memory/android/ManagedMemoryDomain.java'
    managed_before=sha(managed_source.read_bytes())
    source_paths={'memory_core':'src/mgmem.cpp','memory_header':'include/mgmem/mgmem.h',
        'pressure_jni':'adapters/android/native/pressure_jni.cpp',
        'borrow_jni':'adapters/android/native/borrow_jni.cpp',
        'workspace_jni':'adapters/android/native/workspace_jni.cpp',
        'host_jni_cmake':'consumers/java_image/runtime/CMakeLists.txt',
        'consumer_cmake':'consumers/java_image/CMakeLists.txt',
        'pressure_retry_test':'consumers/java_image/tests/PressureRetryJvmTest.java',
        'verification_script':'consumers/java_image/verify_host.py'}
    source_hashes={name:sha((ROOT/path).read_bytes()) for name,path in source_paths.items()}
    compiled=out/'compiled-adapter';compiled.mkdir()
    commands=[]
    def run(label,cmd,env,success):
        r=subprocess.run([str(x) for x in cmd],cwd=ROOT,env=env,capture_output=True,timeout=90,check=False)
        (logs/(label+'.stdout')).write_bytes(r.stdout);(logs/(label+'.stderr')).write_bytes(r.stderr)
        if (r.returncode==0)!=success:raise RuntimeError('unexpected exit for '+label)
        commands.append({'name':label,'exit_code':r.returncode,'stdout_sha256':sha(r.stdout),'stderr_sha256':sha(r.stderr)})
        return r
    env=os.environ.copy()
    dirs=[str(a.core.resolve().parent),str(a.jni.resolve().parent),str(a.consumer_jni.resolve().parent)]
    env['PATH']=os.pathsep.join(dirs+[env.get('PATH','')])
    if os.name!='nt':env['LD_LIBRARY_PATH']=os.pathsep.join(dirs+[env.get('LD_LIBRARY_PATH','')])
    run('compile-current-adapter',[a.javac,'--release','8','-d',compiled,source,managed_source],env,True)
    klass='io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.class'
    managed_klass='io/github/jichuo1/meguviz/memory/android/ManagedMemoryDomain.class'
    with zipfile.ZipFile(a.adapter_jar) as jar:
        adapter_dir=compiled/'io/github/jichuo1/meguviz/memory/android'
        expected={p.name:p.read_bytes() for pattern in ('NativeMemoryLease*.class','ManagedMemoryDomain*.class')
                  for p in adapter_dir.glob(pattern)}
        prefix='io/github/jichuo1/meguviz/memory/android/'
        relevant=[n for n in jar.namelist() if n.startswith(prefix) and n.endswith('.class') and
                  (n[len(prefix):].startswith('NativeMemoryLease') or n[len(prefix):].startswith('ManagedMemoryDomain'))]
        if len(relevant)!=len(set(relevant)) or {n[len(prefix):]:jar.read(n) for n in relevant}!=expected:
            raise RuntimeError('installed Java adapters differ from freshly compiled current sources')
        class_sha=sha(jar.read(klass))
        managed_class_sha=sha(jar.read(managed_klass))
    if sha(source.read_bytes())!=source_before or sha(managed_source.read_bytes())!=managed_before:
        raise RuntimeError('adapter source changed during compilation')
    consumer_source=ROOT/'consumers/java_image/java/io/github/jichuo1/meguviz/memory/image/JavaImage.java'
    consumer_before=sha(consumer_source.read_bytes())
    compiled_consumer=out/'compiled-consumer';compiled_consumer.mkdir()
    run('compile-current-consumer',[a.javac,'--release','8','-classpath',a.adapter_jar,
        '-d',compiled_consumer,consumer_source],env,True)
    class_dir=Path('io/github/jichuo1/meguviz/memory/image')
    fresh={p.name:p.read_bytes() for p in (compiled_consumer/class_dir).glob('JavaImage*.class')}
    actual={p.name:p.read_bytes() for p in (a.classes/class_dir).glob('JavaImage*.class')}
    if not fresh or fresh!=actual or sha(consumer_source.read_bytes())!=consumer_before:
        raise RuntimeError('consumer classes differ from freshly compiled current source')
    java=[a.java,'-Djava.library.path='+os.pathsep.join(dirs),'-cp',
          str(a.classes.resolve())+os.pathsep+str(a.adapter_jar.resolve()),
          'io.github.jichuo1.meguviz.memory.image.JavaImage']
    retry_classes=out/'compiled-retry-test';retry_classes.mkdir()
    retry_source=ROOT/'consumers/java_image/tests/PressureRetryJvmTest.java'
    retry_classpath=str(a.classes.resolve())+os.pathsep+str(a.adapter_jar.resolve())
    run('compile-pressure-retry',[a.javac,'--release','8','-classpath',retry_classpath,
        '-d',retry_classes,retry_source],env,True)
    retry_run=run('pressure-retry-contract',[a.java,'-cp',str(retry_classes)+os.pathsep+retry_classpath,
        'io.github.jichuo1.meguviz.memory.image.PressureRetryJvmTest'],env,True)
    if retry_run.stdout.strip()!=b'PASS_PRESSURE_RETRY_ADMISSION_ONLY':
        raise RuntimeError('pressure retry admission contract mismatch')
    identity=['--attest-core',a.core.resolve(),'--attest-jni',a.jni.resolve(),'--attest-jar',a.adapter_jar.resolve()]
    expected_hashes={'core_sha256':sha(a.core.read_bytes()),'jni_sha256':sha(a.jni.read_bytes()),
                     'java_jar_sha256':sha(a.adapter_jar.read_bytes()),'java_class_sha256':class_sha,
                     'managed_class_sha256':managed_class_sha}
    cases=[]
    pressure_cases=[]
    for index,(w,h,style) in enumerate([(1,1,'plain'),(1,8,'comment'),(8,1,'crlf'),(3,3,'plain'),
                                         (5,7,'comment'),(101,101,'crlf')]):
        rgb=bytearray(((i*73+index*19)&255) for i in range(w*h*3))
        rgb[0]=[0x20,0x23,0x0a,0x0d,0,0xff][index]
        header=(('P6\r\n%d %d\r\n255\r\n'%(w,h)) if style=='crlf' else
                ('P6\n# bounded image\n%d %d\n255\n'%(w,h)) if style=='comment' else
                ('P6\n%d %d\n255\n'%(w,h))).encode('ascii')
        source_file=out/('input-%d.ppm'%index);source_file.write_bytes(header+rgb)
        expected=oracle(w,h,rgb);ref=out/('reference-%d.pgm'%index)
        run('reference-%d'%index,[a.reference,source_file,ref,'--system'],env,True)
        if ref.read_bytes()!=expected:raise RuntimeError('reference and independent pixel oracle differ')
        for mode in ('arena','system'):
            target=out/('result-%d-%s.pgm'%(index,mode));cmd=java+[source_file,target]+identity
            if mode=='system':cmd+=['--system']
            result=run('java-%d-%s'%(index,mode),cmd,env,True)
            report=json.loads(result.stdout.decode('utf-8'))
            if target.read_bytes()!=expected or report.get('success') is not True or report.get('mode')!=mode:
                raise RuntimeError('Java image result differs from reference/oracle')
            expected_counts={'pixels':w*h,'native_final_backing_bytes':0,'managed_cleanup':'CLOSED_AND_FORGOTTEN',
                'native_initialized_bytes':len(rgb),'source_to_java_bytes':len(rgb),
                'java_to_native_staging_bytes':len(rgb),'staging_to_native_backing_bytes':len(rgb),
                'native_to_java_bytes':len(rgb),'java_luma_and_output_bytes':2*w*h,'transfer_buffer_bytes':24576}
            if any(report.get(k)!=v for k,v in expected_counts.items()) or report.get('lease_before_enqueue') is not True or \
                    report.get('identity_probe_loaded') is not True or \
                    report.get('identity_scope')!='OS_MODULE_PATH_AND_CURRENT_FILE_HASH' or \
                    any(report.get(k)!=v for k,v in expected_hashes.items()):
                raise RuntimeError('consumer lifecycle, identity or copying report mismatch')
            if report.get('checksum')!=sum(expected[expected.index(b'255\n')+4:]):raise RuntimeError('checksum mismatch')
            cases.append({'width':w,'height':h,'header':style,'mode':mode,'output_sha256':sha(expected),
                          'checks':'PIXEL_ORACLE_AND_REFERENCE_EXACT_LIFECYCLE_COPY_COUNTS_AND_LOADED_MODULE_IDENTITY'})
            target=out/('pressure-%d-%s.pgm'%(index,mode))
            pressure_cmd=java+[source_file,target]+identity+['--live-domain-pressure']
            if mode=='system':pressure_cmd+=['--system']
            pressure_result=run('java-pressure-%d-%s'%(index,mode),pressure_cmd,env,True)
            pressure_report=json.loads(pressure_result.stdout.decode('utf-8'))
            if target.read_bytes()!=expected or pressure_report.get('success') is not True or pressure_report.get('mode')!=mode:
                raise RuntimeError('live pressure image differs from the independent oracle')
            if any(pressure_report.get(k)!=v for k,v in expected_counts.items()) or \
                    any(pressure_report.get(k)!=v for k,v in expected_hashes.items()) or \
                    pressure_report.get('lease_before_enqueue') is not True or \
                    pressure_report.get('identity_scope')!='OS_MODULE_PATH_AND_CURRENT_FILE_HASH' or \
                    pressure_report.get('pressure_mode')!='LIVE_SYNTHETIC' or \
                    pressure_report.get('pressure_critical')!='APPLIED' or \
                    pressure_report.get('pressure_restore')!='APPLIED' or \
                    pressure_report.get('pressure_budget_after_critical')!=0 or \
                    pressure_report.get('pressure_denied') is not True or \
                    pressure_report.get('pressure_submit_busy_rejections',-1)<0 or \
                    pressure_report.get('pressure_deferred_busy_delta',0)<1 or \
                    pressure_report.get('pressure_post_release_backing')!=0:
                raise RuntimeError('live pressure budget, lease, accounting or identity mismatch')
            if mode=='arena' and (pressure_report.get('pressure_trim_success_delta',0)<1 or
                                  pressure_report.get('pressure_arena_reclaimed') is not True):
                raise RuntimeError('arena did not show a quiet trim after the image lease was released')
            if mode=='system' and pressure_report.get('pressure_arena_reclaimed') is not False:
                raise RuntimeError('system release was incorrectly attributed to arena trim')
            snapshot_calls=pressure_report.get('pressure_snapshot_calls',0)
            ticket_calls=pressure_report.get('pressure_ticket_query_calls',0)
            domain_calls=pressure_report.get('pressure_domain_snapshot_calls',0)
            snapshot_longs=pressure_report.get('pressure_snapshot_long_elements',0)
            ticket_longs=pressure_report.get('pressure_ticket_long_elements',0)
            domain_longs=pressure_report.get('pressure_domain_long_elements',0)
            if min(snapshot_calls,ticket_calls,domain_calls)<=0 or \
                    snapshot_longs<18*snapshot_calls or ticket_longs!=4*ticket_calls or domain_longs!=10*domain_calls or \
                    pressure_report.get('pressure_observation_java_long_payload_bytes')!=8*(snapshot_longs+ticket_longs+domain_longs) or \
                    pressure_report.get('checksum')!=report['checksum']:
                raise RuntimeError('live pressure observation accounting or checksum mismatch')
            pressure_cases.append({'width':w,'height':h,'header':style,'mode':mode,
                                   'output_sha256':sha(expected),'scope':'HOST_SYNTHETIC_PRESSURE_FUNCTIONAL'})
    good=out/'input-0.ppm';original=good.read_bytes()
    # Exclude the identity sidecar from both Java and OS library search paths.
    # A normal Java-only caller must not require a consumer-native owner/probe.
    probe_dir=a.consumer_jni.resolve().parent
    if probe_dir in (a.core.resolve().parent,a.jni.resolve().parent):
        raise RuntimeError('probe_directory_must_be_distinct_for_plain_consumer_check')
    plain_env=env.copy()
    for name in ('PATH','LD_LIBRARY_PATH'):
        parts=[x for x in plain_env.get(name,'').split(os.pathsep) if x]
        plain_env[name]=os.pathsep.join(x for x in parts if Path(x).resolve()!=probe_dir)
    plain_classes=a.plain_classes if a.plain_classes is not None else a.classes
    plain_actual={p.name:p.read_bytes() for p in (plain_classes/class_dir).glob('JavaImage*.class')}
    if fresh!=plain_actual:raise RuntimeError('plain consumer classes differ from current source')
    plain_java=[a.java,'-Djava.library.path='+os.pathsep.join(dirs[:2]),'-cp',
        str(plain_classes.resolve())+os.pathsep+str(a.adapter_jar.resolve()),
        'io.github.jichuo1.meguviz.memory.image.JavaImage']
    plain_output=out/'plain-without-sidecar.pgm'
    plain=run('plain-without-sidecar',plain_java+[good,plain_output],plain_env,True)
    plain_report=json.loads(plain.stdout.decode('utf-8'))
    if plain_output.read_bytes()!=(out/'reference-0.pgm').read_bytes() or \
            plain_report.get('identity_scope')!='NOT_CHECKED' or plain_report.get('identity_probe_loaded') is not False or \
            plain_report.get('managed_cleanup')!='CLOSED_AND_FORGOTTEN':
        raise RuntimeError('plain managed consumer unexpectedly required identity sidecar')
    failures=[('short',original[:-1],[]),('trailing',original+b'x',[]),
              ('budget-system',original,['--system','--budget','1']),('budget-arena',original,['--budget','1']),
              ('pressure-short',original[:-1],['--live-domain-pressure']),
              ('pressure-budget',original,['--live-domain-pressure','--budget','1'])]
    for name,data,extra in failures:
        inp=out/(name+'.ppm');inp.write_bytes(data);target=out/(name+'.pgm')
        r=run('reject-'+name,java+[inp,target]+identity+extra,env,False)
        if target.exists() or b'managed_cleanup=CLOSED_AND_FORGOTTEN' not in r.stderr:raise RuntimeError('failed input cleanup not confirmed or produced output')
    existing=out/'existing.pgm';existing.write_bytes(b'keep-existing')
    r=run('reject-existing-output',java+[good,existing]+identity,env,False)
    if existing.read_bytes()!=b'keep-existing' or b'managed_cleanup=CLOSED_AND_FORGOTTEN' not in r.stderr:
        raise RuntimeError('existing output changed or domain cleanup was not confirmed')
    wrong=out/'wrong.jar';wrong.write_bytes(a.adapter_jar.read_bytes())
    target=out/'wrong-identity.pgm'
    r=run('reject-wrong-loaded-jar',java+[good,target,'--attest-core',a.core.resolve(),
          '--attest-jni',a.jni.resolve(),'--attest-jar',wrong.resolve()],env,False)
    if target.exists() or b'managed_cleanup=NO_DOMAIN_ATTEMPTED' not in r.stderr:raise RuntimeError('wrong JAR identity accepted')
    target=out/'wrong-core.pgm'
    r=run('reject-wrong-loaded-core',java+[good,target,'--attest-core',a.jni.resolve(),
          '--attest-jni',a.jni.resolve(),'--attest-jar',a.adapter_jar.resolve()],env,False)
    if target.exists() or b'managed_cleanup=NO_DOMAIN_ATTEMPTED' not in r.stderr:raise RuntimeError('wrong core identity accepted')
    target=out/'missing-option.pgm'
    r=run('reject-missing-option',java+[good,target,'--budget'],env,False)
    if target.exists() or b'IllegalArgumentException' not in r.stderr or b'managed_cleanup=NO_DOMAIN_ATTEMPTED' not in r.stderr:
        raise RuntimeError('missing option did not fail safely')
    if sha(source.read_bytes())!=source_before or sha(managed_source.read_bytes())!=managed_before or sha(consumer_source.read_bytes())!=consumer_before or any(
            sha((ROOT/path).read_bytes())!=source_hashes[name] for name,path in source_paths.items()):
        raise RuntimeError('source changed during host checks')
    receipt={'state':'PASS_HOST_FUNCTIONAL_DEVELOPMENT','scope':'INSTALLED_SHARED_CORE_AND_HOST_JNI_NO_ANDROID_NO_PERFORMANCE',
             'cases':cases,'pressure_cases':pressure_cases,'pressure_retry_contract':'PASS_JVM_ADMISSION_ONLY',
             'plain_without_sidecar':'PASS','negative_cases':['truncated','trailing','budget_system','budget_arena','pressure_short','pressure_budget',
                 'existing_output_preserved','wrong_loaded_jar','wrong_loaded_core','missing_option'],
             'identities':expected_hashes,'adapter_java_source_sha256':source_before,'managed_java_source_sha256':managed_before,
             'consumer_source_sha256':consumer_before,
             'native_source_sha256':sha((ROOT/'consumers/java_image/native/image_jni.cpp').read_bytes()),
             'consumer_native_sha256':sha(a.consumer_jni.read_bytes()),'reference_source_sha256':sha((ROOT/'consumers/image/main.cpp').read_bytes()),
             'reference_binary_sha256':sha(a.reference.read_bytes()),'observed_source_sha256':source_hashes,'commands':commands,
             'limitations':['Current loaded module paths are compared by the OS and current file bytes are hashed; this is not a hash of mapped memory.',
                'The host JNI library is separately built from adapter source and is not the Android AAR JNI binary.',
                'Source and executable hashes identify the observed inputs; hashes alone do not prove the native build provenance.',
                'Java arrays and JNI staging are extra copies; no runtime, memory, energy or BIL benefit is claimed.',
                'An I/O failure after a new output file is created may leave a partial file; existing files are never overwritten.',
                'No Android/ART/device or performance qualification is implied.']}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'state':receipt['state'],'functional_cases':len(cases),'negative_cases':len(failures)+4}))


if __name__=='__main__':main()
