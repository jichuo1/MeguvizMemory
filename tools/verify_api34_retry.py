"""Bounded R8 retryfix2 guard and lifecycle smoke on a newly owned API34 AVD.

Never restarts adb, clears app data or mutates another device/app. Transport
timeout or a foreign instrumentation is UNKNOWN/BLOCKED, with no force-stop.
Raw device inventories and instrumentation streams remain in ignored build/.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from run_arm64_validation import BUSY_PROCESS

ROOT=Path(__file__).resolve().parents[1]
PACKAGE='io.github.jichuo1.meguviz.memory.pressureprobe.r8'
COMPONENT=PACKAGE+'/io.github.jichuo1.meguviz.memory.android.PressureR8Instrumentation'
ADB=Path.home()/'AppData/Local/Android/Sdk/platform-tools/adb.exe'
SDK=Path.home()/'AppData/Local/Android/Sdk'
JAVA=Path('C:/Program Files/Microsoft/jdk-17.0.20.101-hotspot/bin/java.exe')
SERIAL=os.environ.get('MGMEM_API34_SERIAL','')
BASE=ROOT/'artifacts/android-close-investigation-20261030'
PRIVATE=ROOT/'build/android-close-api34-avd/private-validation'
BUILD=BASE/'retry-r8-x86_64-build.json'
NATIVE_BUILD=BASE/'retry-arm64-aar-build.json'

def sha(b):return hashlib.sha256(b).hexdigest()
def filehash(p):return sha(p.read_bytes())
def utc():return datetime.now(timezone.utc).isoformat()
def require(ok,why):
    if not ok:raise RuntimeError(why)
def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.pending')
    with temp.open('wb') as stream:
        stream.write((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode());stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)
def command(name,args,timeout=20,required=True):
    start=time.monotonic()
    try:
        p=subprocess.run([str(ADB),'-s',SERIAL,*args],capture_output=True,timeout=timeout)
        result={'name':name,'exit_code':p.returncode,'timed_out':False,'elapsed_seconds':time.monotonic()-start,
                'stdout_sha256':sha(p.stdout),'stderr_sha256':sha(p.stderr)}
        stdout,stderr=p.stdout,p.stderr
    except subprocess.TimeoutExpired as error:
        stdout,stderr=error.stdout or b'',error.stderr or b''
        result={'name':name,'exit_code':None,'timed_out':True,'remote_state':'UNKNOWN',
                'stdout_sha256':sha(stdout),'stderr_sha256':sha(stderr)}
    (PRIVATE/(name+'.stdout')).write_bytes(stdout);(PRIVATE/(name+'.stderr')).write_bytes(stderr)
    if required:require(result['exit_code']==0 and not result['timed_out'],name+'_failed_or_unknown')
    return result,stdout.decode('utf-8','replace')

def occupancy(name):
    psrow,ps=command(name+'-ps',['shell','ps','-A','-o','PID,NAME,ARGS'])
    parsed=[parts for line in ps.splitlines() if len(parts:=line.split(None,2))>=2 and parts[0].isdigit()]
    require(parsed,'process_inventory_unparseable')
    busy=[p for p in parsed if BUSY_PROCESS.search(' '.join(p[1:])) or 'am instrument' in ' '.join(p[1:])]
    actrow,activity=command(name+'-activity',['shell','dumpsys','activity'])
    active=[line for line in activity.splitlines() if 'ActiveInstrumentation{' in line or
            (re.search(r'\bmActiveInstrumentation\s*[:=]',line) and not re.search(r'[:=]\s*(?:null|None|\[\]|\{\}|0)\s*$',line))]
    result={'scope':'SUCCESSFUL_PROCESS_AND_ACTIVITY_DUMP_FILTERED_IN_MEMORY','process_rows':len(parsed),
            'matching_tests':len(busy),'active_instrumentation':len(active),
            'process_inventory_sha256':psrow['stdout_sha256'],'activity_inventory_sha256':actrow['stdout_sha256'],
            'result':'PASS_OBSERVATIONAL_ONLY' if not busy and not active else 'BLOCKED'}
    require(result['result']=='PASS_OBSERVATIONAL_ONLY',name+'_other_test_or_instrumentation')
    return result

def build_identity():
    built=json.loads(BUILD.read_text(encoding='utf-8'));native=json.loads(NATIVE_BUILD.read_text(encoding='utf-8'))
    require(built['status']=='PASS' and built['abi']=='x86_64' and built['package_name']==PACKAGE,'wrong_build_receipt')
    for entry in built['sources']+built['native_overrides']+built['artifacts']+[built['input_aar'],built['base_aar']]+native['sources']:
        path=(ROOT/entry['path']).resolve();require(path.is_relative_to(ROOT) and path.is_file() and filehash(path)==entry['sha256'],'build_input_drift')
    apk=next(ROOT/item['path'] for item in built['artifacts'] if item['path'].endswith('MeguvizMemory-R8-Stability-Probe.apk'))
    with zipfile.ZipFile(apk) as z:
        require(z.testzip() is None,'APK_corrupt')
        for item in built['native_overrides']:
            name='libmgmem_pressure_jni.so' if item['kind']=='jni' else 'libmgmem.so'
            data=z.read('lib/x86_64/'+name)
            require(sha(data)==item['sha256'],'APK_DSO_identity_mismatch')
            require(data[:6]==b'\x7fELF\x02\x01' and struct.unpack_from('<H',data,18)[0]==62,'APK_DSO_wrong_arch')
    cert=subprocess.run([str(JAVA),'-jar',str(SDK/'build-tools/37.0.0/lib/apksigner.jar'),'verify','--print-certs',str(apk)],capture_output=True,text=True,timeout=30,check=True)
    certs=re.findall(r'certificate SHA-256 digest:\s*([a-fA-F0-9]{64})',cert.stdout)
    require(len(certs)==1 and certs[0].lower()==built['certificate_sha256'],'APK_certificate_mismatch')
    badging=subprocess.run([str(SDK/'build-tools/37.0.0/aapt2.exe'),'dump','badging',str(apk)],capture_output=True,text=True,timeout=30,check=True)
    require(re.findall(r"(?m)^package: name='([^']+)'",badging.stdout)==[PACKAGE],'APK_package_mismatch')
    evidence=built['r8_evidence']
    require(all(evidence[k] for k in ('shrinking_enabled','optimization_enabled','obfuscation_enabled')),'R8_not_enabled')
    return built,apk,{'apk_sha256':filehash(apk),'build_receipt_sha256':filehash(BUILD),'native_build_receipt_sha256':filehash(NATIVE_BUILD),
                      'certificate_sha256':built['certificate_sha256'],'package':PACKAGE,
                      'native_overrides':built['native_overrides'],'r8_marker_after':evidence['marker_after']}

def read_cache(name):
    row,text=command('cache-'+name.replace('/','-'),['shell','run-as',PACKAGE,'cat','cache/'+name],required=False)
    if row['exit_code']!=0:return None
    try:return json.loads(text)
    except (ValueError,TypeError):return None

def cgroup_sample(pid):
    # Only the current own process; paths/UID/PID never enter public receipts.
    try:
        _,cmd=command('sample-cmdline-'+str(time.monotonic_ns()),['shell','run-as',PACKAGE,'cat',f'/proc/{pid}/cmdline'])
        require(cmd.split('\x00')[0]==PACKAGE,'own_process_identity_changed')
        _,status=command('sample-status-'+str(time.monotonic_ns()),['shell','run-as',PACKAGE,'cat',f'/proc/{pid}/status'])
        uid=re.search(r'(?m)^Uid:\s*(\d+)',status);require(uid is not None,'own_uid_unavailable')
        expected=f'/uid_{uid.group(1)}/pid_{pid}'
        _,lines=command('sample-cgroup-'+str(time.monotonic_ns()),['shell','run-as',PACKAGE,'cat',f'/proc/{pid}/cgroup'])
        require('0::'+expected in lines.splitlines(),'own_pid_cgroup_not_visible')
        path='/sys/fs/cgroup'+expected
        _,freeze=command('sample-freeze-'+str(time.monotonic_ns()),['shell','cat',path+'/cgroup.freeze'])
        _,events=command('sample-events-'+str(time.monotonic_ns()),['shell','cat',path+'/cgroup.events'])
        frozen=re.search(r'(?m)^frozen\s+([01])\s*$',events)
        require(freeze.strip() in ('0','1') and frozen,'freezer_fields_unavailable')
        return {'at_utc':utc(),'state':'OBSERVED','cgroup_freeze':int(freeze.strip()),'cgroup_events_frozen':int(frozen.group(1)),
                'scope':'OWN_PID_ONLY_NOT_CONTINUOUS','freeze_controller_modified':False}
    except (RuntimeError,subprocess.SubprocessError):
        return {'at_utc':utc(),'state':'UNAVAILABLE','reason':'OWN_PID_CGROUP_OR_FREEZER_NOT_READABLE','scope':'OWN_PID_ONLY'}

def main():
    global PRIVATE
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume-prior-guard',action='store_true',help='Continue after the saved first guard using the same unchanged own APK/cache')
    parser.add_argument('--resume-label',default='resume2',help='New scoped label; prior attempts are not overwritten')
    options=parser.parse_args()
    if not re.fullmatch(r'emulator-[0-9]{4,5}',SERIAL):
        parser.error('Set MGMEM_API34_SERIAL to the explicitly owned emulator')
    require(re.fullmatch(r'resume[0-9]+',options.resume_label),'invalid_resume_label')
    prior_private=PRIVATE
    if options.resume_prior_guard:PRIVATE=ROOT/'build/android-close-api34-avd'/('private-validation-'+options.resume_label)
    require(not PRIVATE.exists(),'private_validation_directory_must_be_new');PRIVATE.mkdir(parents=True)
    guardpath=BASE/('api34-retry-guard-'+options.resume_label if options.resume_prior_guard else 'api34-retry-guard')/'receipt.json'
    normalpath=BASE/('api34-retry-smoke-'+options.resume_label if options.resume_prior_guard else 'api34-retry-smoke')/'receipt.json'
    require(not guardpath.exists() and not normalpath.exists(),'receipt_directory_must_be_new')
    receipt={'state':'PREPARING','started_at_utc':utc(),'scope':'ISOLATED_OWN_API34_EMULATOR_FUNCTIONAL_ONLY',
             'source_sha256':filehash(Path(__file__)),'device_serial_sha256':sha(SERIAL.encode()),'gates':[],
             'shared_device_lock':False,'performance':'NOT_MEASURED','BIL':'NOT_INTEGRATED'}
    guard={'state':'NOT_RUN'};normal={'state':'NOT_RUN'}
    try:
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            _,boot=command('boot-poll-'+str(time.monotonic_ns()),['shell','getprop','sys.boot_completed'],timeout=8,required=False)
            if boot.strip()=='1':break
            time.sleep(3)
        else:raise RuntimeError('owned_emulator_boot_timeout')
        _,avd=command('avd-name',['emu','avd','name']);require(avd.splitlines()[0].strip()=='MemoryRetryApi34','AVD_identity_mismatch')
        receipt['gates'].append(occupancy('before-install'))
        metadata={}
        for name,args in {'api':['shell','getprop','ro.build.version.sdk'],'abi':['shell','getprop','ro.product.cpu.abi'],
                          'pages':['shell','getconf','PAGESIZE'],'boot':['shell','cat','/proc/sys/kernel/random/boot_id']}.items():
            _,text=command('device-'+name,args);metadata[name]=text.strip()
        require(metadata['api']=='34' and metadata['abi']=='x86_64','API34_x86_64_required')
        receipt['device']={'api':34,'abi':'x86_64','page_bytes':int(metadata['pages']),'boot_sha256':sha(metadata['boot'].encode())}
        built,apk,receipt['APK']=build_identity()
        row,path=command('own-package-before',['shell','pm','path',PACKAGE],required=False)
        if options.resume_prior_guard:
            paths=re.findall(r'(?m)^package:(/data/app/[A-Za-z0-9_./=+~-]+\.apk)\r?$',path)
            require(row['exit_code']==0 and len(paths)==1,'expected_one_previously_installed_own_APK')
            pulled=PRIVATE/'installed-own.apk';command('pull-own-package',['pull',paths[0],str(pulled)])
            require(filehash(pulled)==filehash(apk),'installed_own_APK_changed')
            previous=BASE/'api34-retry-overview.json'
            previous_value=json.loads(previous.read_text(encoding='utf-8'))
            old_source=BASE/'api34-retry-guard/executed-runner-v1.txt'
            require(previous_value.get('reason')=='guard_falsely_finished_as_success' and
                    filehash(old_source)==previous_value['source_sha256'],'prior_guard_provenance_missing')
            old_output=prior_private/'guard-instrumentation.stdout';stdout=old_output.read_text(encoding='utf-8')
            row={'exit_code':None,'timed_out':False,'stdout_sha256':filehash(old_output)}
            receipt['resume_provenance']={'first_controller_receipt_sha256':filehash(previous),
                'exact_first_controller_source_sha256':filehash(old_source),
                'first_guard_stdout_sha256':filehash(old_output),'guard_repeated':False}
        else:
            require(row['exit_code']==1 and not path.strip(),'package_must_not_preexist_on_new_AVD')
            row,installed=command('install-own-package',['install',str(apk)],timeout=120)
            require('Success' in installed,'own_APK_not_installed')
            # Fresh AVD + first install: reject stale guard state instead of clearing it.
            require(read_cache('mgmem-r8-stall.json') is None,'unexpected_preexisting_stall_cache')
            receipt['gates'].append(occupancy('before-guard'))
            row,stdout=command('guard-instrumentation',['shell','am','instrument','-w','-r','-e','iterations','3',
                '-e','inject_progress_lock_timeout','true','-e','stall_timeout_ms','3000','-e','capture_stack_on_halt','true',
                '-e','max_duration_ms','15000','-e','observe_ams','false',COMPONENT],timeout=30,required=False)
            require(not row['timed_out'],'guard_transport_unknown_no_kill')
        stall=read_cache('mgmem-r8-stall.json');require(isinstance(stall,dict),'guard_stall_cache_missing')
        require(stall.get('diagnostic_revision')==4 and stall.get('status')=='UNKNOWN' and stall.get('unsafe_cleanup') is True,'guard_unknown_state')
        require(stall.get('phase')=='diagnostic.inject-progress-lock.blocked' and stall.get('reason')=='STALL_PHASE_TIMEOUT','guard_wrong_boundary')
        require(stall.get('boundary_outcome')=='UNKNOWN_RUNNING_WORK' and stall.get('native_resource_state')=='UNKNOWN_NOT_FORCED_FREE','guard_false_zero')
        progress=read_cache('mgmem-r8-progress.json');require(progress and progress.get('run_id')==stall.get('run_id'),'guard_run_identity_mismatch')
        # Android may print CODE:0 even when the process crashes. A shortMsg
        # crash and an UNKNOWN cache, without a PASS report, define this guard.
        require('shortMsg=Process crashed.' in stdout and not re.search(r'(?m)^INSTRUMENTATION_RESULT: report_json=.*"status"\s*:\s*"PASS"',stdout),'guard_not_expected_process_isolation')
        row_pid,pidtext=command('guard-own-pid-after',['shell','pidof',PACKAGE],required=False)
        require(not pidtext.strip(),'guard_expected_own_process_to_exit')
        threads=stall.get('threads',[])
        guard={'state':'PASS_EXPECTED_FAIL_STOP_UNKNOWN','normal_domain_close_qualified':False,'own_run_id_sha256':sha(stall['run_id'].encode()),
               'phase':stall['phase'],'reason':stall['reason'],'elapsed_ms':stall['elapsed_ms'],
               'unsafe_cleanup':stall['unsafe_cleanup'],'boundary_outcome':stall['boundary_outcome'],
               'native_resource_state':stall['native_resource_state'],'stack_status':stall.get('stack_status','UNAVAILABLE'),
               'observed_thread_count':stall.get('observed_threads'),'stack_thread_names':[t.get('name') for t in threads if t.get('name') in ('main','Instrumentation','memory-r8-progress-lock-holder','memory-r8-wall-watchdog')],
               'instrumentation_exit_code':row['exit_code'],'stdout_sha256':row['stdout_sha256'],'device':receipt['device'],'APK':receipt['APK'],
               'initial_controller_correction':'CODE_0_CAN_ACCOMPANY_PROCESS_CRASH; VALIDATED_OWN_RUN_ID_AND_UNKNOWN_CACHE',
               'no_forced_free':True,'performance':'NOT_MEASURED'}
        (PRIVATE/'guard-stall.json').write_text(json.dumps(stall,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        receipt['gates'].append(occupancy('before-normal'));save(guardpath,guard)
        # Separate process/run id; explicit AMS opt-out and no injected fault.
        rawout=(PRIVATE/'normal-instrumentation.stdout').open('wb');rawerr=(PRIVATE/'normal-instrumentation.stderr').open('wb')
        args=[str(ADB),'-s',SERIAL,'shell','am','instrument','-w','-r','-e','iterations','20',
              '-e','min_duration_ms','8000','-e','max_duration_ms','60000','-e','sample_every','20',
              '-e','observe_ams','false','-e','stall_timeout_ms','10000',COMPONENT]
        child=subprocess.Popen(args,stdout=rawout,stderr=rawerr);start=time.monotonic();samples=[];last_sample=0;normal_pid=None
        while child.poll() is None and time.monotonic()-start<75:
            if len(samples)<3 and time.monotonic()-last_sample>=2:
                row,pids=command('normal-pid-'+str(time.monotonic_ns()),['shell','pidof',PACKAGE],timeout=8,required=False)
                values=pids.split()
                if len(values)==1 and values[0].isdigit():
                    normal_pid=values[0];samples.append(cgroup_sample(normal_pid));last_sample=time.monotonic()
            time.sleep(.25)
        if child.poll() is None:
            normal={'state':'BLOCKED_OR_INCOMPLETE','reason':'NORMAL_TRANSPORT_TIMEOUT_REMOTE_UNKNOWN','cgroup_samples':samples}
            save(normalpath,normal);raise RuntimeError('normal_transport_unknown_no_kill')
        rawout.close();rawerr.close();output=(PRIVATE/'normal-instrumentation.stdout').read_text(encoding='utf-8',errors='replace')
        matches=re.findall(r'(?m)^INSTRUMENTATION_RESULT: report_json=(\{.*\})\r?$',output)
        require(child.returncode==0 and len(matches)==1,'normal_report_missing')
        report=json.loads(matches[0]);require(report.get('status')=='PASS' and re.search(r'(?m)^INSTRUMENTATION_CODE: 0\s*$',output),'normal_test_not_PASS')
        require(report['diagnostic_revision']==4 and report['run_id']!=stall['run_id'],'normal_run_id_not_new')
        require(report['api']==34 and report['r8_marker_runtime_name']==built['r8_evidence']['marker_after'],'normal_api_or_R8_mismatch')
        require(report['iterations_completed']>=20 and report['actual_duration_ms']>=8000,'normal_count_or_duration')
        require(report['fire_and_forget_state']=='PASS_REGISTRATION_REMOVED_WITHOUT_CALLER_POLL','fire_and_forget_not_proven')
        require(report['final_backing_bytes_each_domain']==0 and report['performance_qualified'] is False and report['actual_low_memory_event'] is False,'normal_final_scope')
        require(not report['inject_progress_lock_timeout'] and not report['inject_main_running_timeout'],'normal_fault_injection_not_disabled')
        require(all(s.get('cgroup_freeze',0)==0 and s.get('cgroup_events_frozen',0)==0 for s in samples),'own_process_frozen_observed')
        (PRIVATE/'normal-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        normal_run_hash=sha(report['run_id'].encode())
        report.pop('process_pid',None);report.pop('run_id',None)
        normal={'state':'PASS_SCOPED_API34_EMULATOR_FUNCTIONAL','device':receipt['device'],'APK':receipt['APK'],
                'instrumentation_report':report,'own_run_id_sha256':normal_run_hash,
                'guard_run_id_differs':True,'cgroup_samples':samples,
                'cgroup_scope':'OBSERVATIONAL_SAMPLES_NOT_CONTINUOUS_PROOF',
                'instrumentation_stdout_sha256':filehash(PRIVATE/'normal-instrumentation.stdout'),
                'native_vm_retry_failure_injection':'HOST_FAKE_VM_ONLY_NOT_THIS_DEVICE',
                'BIL':'NOT_INTEGRATED','performance':'NOT_MEASURED','energy':'UNAVAILABLE'}
        normal['postflight_gate']=occupancy('after-normal');save(normalpath,normal)
        receipt['state']='PASS';receipt['guard_receipt_sha256']=filehash(guardpath);receipt['normal_receipt_sha256']=filehash(normalpath)
    except Exception as error:
        receipt.update(state='BLOCKED_OR_FAILED',reason=str(error),finished_at_utc=utc());save(BASE/('api34-retry-overview-'+options.resume_label+'.json' if options.resume_prior_guard else 'api34-retry-overview.json'),receipt)
        if guard['state']=='NOT_RUN':save(guardpath,{'state':'BLOCKED_OR_NOT_RUN','reason':str(error)})
        if normal['state']=='NOT_RUN':save(normalpath,{'state':'BLOCKED_OR_NOT_RUN','reason':str(error)})
        print(json.dumps({'state':receipt['state'],'reason':str(error)}),flush=True);raise
    receipt['finished_at_utc']=utc();save(BASE/('api34-retry-overview-'+options.resume_label+'.json' if options.resume_prior_guard else 'api34-retry-overview.json'),receipt)
    print(json.dumps({'state':'PASS','guard':guard['state'],'normal_iterations':normal['instrumentation_report']['iterations_completed'],
                      'cgroup_samples':[s['state'] for s in normal['cgroup_samples']]}),flush=True)

if __name__=='__main__':main()
