"""Observe the previously stalled debuggable consumer on one admitted device.

This is a bounded functional diagnostic, not a performance run. Raw commands
and the installed APK stay in an ignored workspace build directory; public
receipts contain only hashes, own-package progress, and filtered gate results.
Never stop a different package or clear app data. A timeout is UNKNOWN.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zipfile
from run_arm64_validation import BUSY_PROCESS

ROOT=Path(__file__).resolve().parents[1]
PACKAGE='io.github.jichuo1.meguviz.memory.pressureprobe.r8'
COMPONENT=PACKAGE+'/io.github.jichuo1.meguviz.memory.android.PressureR8Instrumentation'
APK=ROOT/'build/continuation-r8-arm64-closefix/MeguvizMemory-R8-Stability-Probe.apk'
BUILD=ROOT/'artifacts/continuation-20261030/android-build/r8-arm64-closefix-build.json'

def digest(data):return hashlib.sha256(data).hexdigest()
def file_hash(path):return digest(path.read_bytes())
def now():return datetime.now(timezone.utc).isoformat()
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb',type=Path,required=True);parser.add_argument('--serial',required=True)
    parser.add_argument('--out-dir',type=Path,required=True);parser.add_argument('--private-dir',type=Path,required=True)
    parser.add_argument('--minimum-ms',type=int,default=60000);parser.add_argument('--wall-ms',type=int,default=180000)
    parser.add_argument('--stall-seconds',type=int,default=12);parser.add_argument('--overall-seconds',type=int,default=240)
    args=parser.parse_args();out=args.out_dir.resolve();private=args.private_dir.resolve()
    if not out.is_relative_to(ROOT/'artifacts') or out.exists():parser.error('new artifact directory required')
    if not private.is_relative_to(ROOT/'build') or private.exists():parser.error('new private build directory required')
    if not 0<=args.minimum_ms<args.wall_ms<=360000 or not 5<=args.stall_seconds<=60 or not 60<=args.overall_seconds<=600:parser.error('invalid bounds')
    out.mkdir(parents=True);private.mkdir(parents=True)
    receipt={'state':'PREPARING','scope':'OWN_DEBUGGABLE_APK_FUNCTIONAL_DIAGNOSTIC_NOT_PERFORMANCE',
      'source_sha256':file_hash(Path(__file__)),'device_serial_sha256':digest(args.serial.encode()),'started_at_utc':now(),
      'prior_user_authorization':'CURRENT_USER_REQUEST_AND_PRIOR_NEW_DEVICE_AUTHORIZATION',
      'shared_device_lock':False,'raw_data':'IGNORED_BUILD_DIRECTORY','gates':[],'commands':[],
      'limits':{'minimum_ms':args.minimum_ms,'wall_ms':args.wall_ms,'stall_seconds':args.stall_seconds,'overall_seconds':args.overall_seconds},
      'BIL':'NOT_MODIFIED_NOT_INTEGRATED','performance':'NOT_MEASURED'}
    def save():
        temp=out/'receipt.pending';data=(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n').encode()
        with temp.open('wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,out/'receipt.json')
    def adb(label,command,timeout=20,required=True):
        started=time.monotonic()
        try:
            p=subprocess.run([str(args.adb),'-s',args.serial,*command],capture_output=True,timeout=timeout)
            row={'name':label,'exit_code':p.returncode,'timed_out':False,'stdout_sha256':digest(p.stdout),'stderr_sha256':digest(p.stderr),'elapsed_seconds':time.monotonic()-started}
            stdout,stderr=p.stdout,p.stderr
        except subprocess.TimeoutExpired as error:
            stdout,stderr=error.stdout or b'',error.stderr or b'';row={'name':label,'exit_code':None,'timed_out':True,'stdout_sha256':digest(stdout),'stderr_sha256':digest(stderr),'remote_process_state':'UNKNOWN'}
        (private/(label+'.stdout')).write_bytes(stdout);(private/(label+'.stderr')).write_bytes(stderr)
        receipt['commands'].append(row);save()
        if required and (row['exit_code']!=0 or row['timed_out']):raise RuntimeError(label+'_command_failed_or_unknown')
        return row,stdout.decode('utf-8','replace')
    def occupancy(label):
        _,ps=adb(label+'-ps',['shell','ps','-A','-o','PID,NAME,ARGS'])
        lines=ps.splitlines();rows=[line.split(None,2) for line in lines if line.strip()]
        parsed=[row for row in rows if len(row)>=2 and row[0].isdigit()]
        if not parsed:raise RuntimeError('cannot_parse_process_inventory')
        busy=[row for row in parsed if BUSY_PROCESS.search(' '.join(row[1:])) or 'am instrument' in ' '.join(row[1:])]
        _,activity=adb(label+'-activity',['shell','dumpsys','activity'])
        active=[line for line in activity.splitlines() if 'ActiveInstrumentation{' in line or
                (re.search(r'\bmActiveInstrumentation\s*[:=]',line) and not re.search(r'[:=]\s*(?:null|None|\[\]|\{\}|0)\s*$',line))]
        receipt['gates'].append({'name':label,'parsed_processes':len(parsed),'matching_tests':len(busy),'active_instrumentation':len(active),
                                 'result':'PASS_OBSERVATIONAL_ONLY' if not busy and not active else 'BLOCKED'})
        save()
        if busy or active:raise RuntimeError('device_busy_other_test_or_instrumentation')
    save();child=None
    try:
        occupancy('initial')
        metadata={}
        for key,command in {'api':['shell','getprop','ro.build.version.sdk'],'abi':['shell','getprop','ro.product.cpu.abi'],
                            'model':['shell','getprop','ro.product.model'],'page_bytes':['shell','getconf','PAGESIZE'],
                            'boot':['shell','cat','/proc/sys/kernel/random/boot_id']}.items():
            _,value=adb('device-'+key,command);metadata[key]=value.strip()
        if metadata['api']!='33' or metadata['abi']!='arm64-v8a' or metadata['page_bytes']!='4096':raise RuntimeError('device_profile_differs_from_stalled_device')
        receipt['device']={k:(digest(v.encode()) if k=='boot' else v) for k,v in metadata.items()}
        built=json.loads(BUILD.read_text(encoding='utf-8'));expected=next(item for item in built['artifacts'] if item['path'].endswith('.apk'))
        if file_hash(APK)!=expected['sha256'] or built['package_name']!=PACKAGE:raise RuntimeError('local_APK_identity_mismatch')
        _,path_text=adb('installed-package',['shell','pm','path',PACKAGE]);paths=re.findall(r'(?m)^package:(/data/app/[A-Za-z0-9_./=+~-]+\.apk)\r?$',path_text)
        if len(paths)!=1:raise RuntimeError('expected_one_existing_own_APK')
        pulled=private/'installed.apk';adb('pull-installed',['pull',paths[0],str(pulled)])
        if file_hash(pulled)!=file_hash(APK):raise RuntimeError('installed_APK_differs_no_replacement_attempted')
        with zipfile.ZipFile(APK) as z:
            core=digest(z.read('lib/arm64-v8a/libmgmem.so'));jni=digest(z.read('lib/arm64-v8a/libmgmem_pressure_jni.so'))
        if core!=file_hash(ROOT/'install/features-arm64/lib/libmgmem.so') or jni!=file_hash(ROOT/'build/continuation-pressure-jni-arm64-closefix/libmgmem_pressure_jni.so'):
            raise RuntimeError('installed_shared_native_closure_mismatch')
        receipt['APK']={'sha256':file_hash(APK),'build_receipt_sha256':file_hash(BUILD),'core_sha256':core,'jni_sha256':jni,'package':PACKAGE}
        _,old=adb('previous-progress',['shell','run-as',PACKAGE,'cat','cache/mgmem-r8-progress.json'],required=False)
        try:old_id=json.loads(old).get('run_id')
        except (ValueError,TypeError):old_id=None
        occupancy('immediately-before-instrumentation')
        stdout=(private/'instrumentation.stdout').open('wb');stderr=(private/'instrumentation.stderr').open('wb')
        command=[str(args.adb),'-s',args.serial,'shell','am','instrument','-w','-r','-e','iterations','1000',
                 '-e','min_duration_ms',str(args.minimum_ms),'-e','max_duration_ms',str(args.wall_ms),
                 '-e','sample_every','200','-e','observe_ams','false',COMPONENT]
        child=subprocess.Popen(command,stdout=stdout,stderr=stderr);receipt['state']='RUNNING';receipt['started_instrumentation_at_utc']=now();save()
        started=time.monotonic();last_changed=started;last=None;last_id=None;stall=False
        print(json.dumps({'state':'RUNNING','receipt':out.relative_to(ROOT).as_posix()}),flush=True)
        while time.monotonic()-started<args.overall_seconds:
            if child.poll() is not None:break
            try:
                p=subprocess.run([str(args.adb),'-s',args.serial,'shell','run-as',PACKAGE,'cat','cache/mgmem-r8-progress.json'],capture_output=True,timeout=8)
                value=json.loads(p.stdout) if p.returncode==0 else None
            except (subprocess.TimeoutExpired,ValueError):value=None
            if value and value.get('run_id')!=old_id and value.get('diagnostic_revision')==3:
                current=digest(json.dumps(value,sort_keys=True).encode());last_id=value['run_id']
                if current!=last:last=current;last_changed=time.monotonic();receipt['last_own_progress']=value;save()
                if not stall and time.monotonic()-last_changed>=args.stall_seconds:
                    stall=True;receipt['state']='OBSERVED_STALL_NOT_CAUSAL_DIAGNOSIS';receipt['stall_at_utc']=now();receipt['unchanged_seconds']=time.monotonic()-last_changed
                    pid,pidout=adb('own-pid-at-stall',['shell','pidof',PACKAGE]);pids=pidout.split()
                    if len(pids)==1 and pids[0].isdigit():
                        (private/'own-pid.txt').write_text(pids[0],encoding='ascii');receipt['own_process_pid_sha256']=digest(pids[0].encode())
                    save();print(json.dumps({'state':'OBSERVED_STALL','progress':value,'own_pid_available':len(pids)==1}),flush=True)
            if (private/'stop-request').exists():receipt['stop_reason']='OWNER_DIAGNOSTIC_CONTROL';break
            time.sleep(1)
        else:receipt['stop_reason']='OVERALL_BOUND'
        if child.poll() is None:
            # Only our uniquely named diagnostic process, never app data.
            adb('stop-own-diagnostic',['shell','am','force-stop',PACKAGE]);receipt['normal_close_proven']=False
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:receipt['remote_process_state']='UNKNOWN';child.terminate()
        stdout.close();stderr.close()
        receipt['instrumentation_exit_code']=child.returncode;receipt['elapsed_seconds']=time.monotonic()-started
        receipt['instrumentation_stdout_sha256']=file_hash(private/'instrumentation.stdout');receipt['instrumentation_stderr_sha256']=file_hash(private/'instrumentation.stderr')
        receipt['state']='STALL_OBSERVED_INCOMPLETE' if stall else 'COMPLETED_OR_STOPPED_NEEDS_REPORT_REVIEW'
        receipt['finished_at_utc']=now();save();print(json.dumps({'state':receipt['state']}),flush=True)
    except Exception as error:
        receipt.update(state='BLOCKED_OR_FAILED',reason=str(error),finished_at_utc=now());save();print(json.dumps({'state':receipt['state'],'reason':str(error)}),flush=True)
        raise

if __name__=='__main__':main()
