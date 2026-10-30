"""Admitted Android close diagnostic with own-process cgroup observation.

Uses a known, same-signer diagnostic package only. A frozen cgroup invalidates
the run; it is never forcibly thawed. No global policy, user data, unrelated
process or BIL dependency is changed. Raw transports remain in ignored build/.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib,json,os,re,subprocess,time,zipfile
from pathlib import Path
from run_arm64_validation import BUSY_PROCESS
from run_continuation_device import android_checks

ROOT=Path(__file__).resolve().parents[1]
PACKAGE='io.github.jichuo1.meguviz.memory.pressureprobe.r8'
COMPONENT=PACKAGE+'/io.github.jichuo1.meguviz.memory.android.PressureR8Instrumentation'
ACTIVITY=PACKAGE+'/io.github.jichuo1.meguviz.memory.android.PressureR8ForegroundActivity'
def sha(data):return hashlib.sha256(data).hexdigest()
def digest(path):return sha(path.read_bytes())
def now():return datetime.now(timezone.utc).isoformat()
def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--adb',type=Path,required=True);ap.add_argument('--serial',required=True)
    ap.add_argument('--sdk',type=Path,required=True);ap.add_argument('--java-home',type=Path,required=True)
    ap.add_argument('--build-receipt',type=Path,required=True);ap.add_argument('--out-dir',type=Path,required=True);ap.add_argument('--private-dir',type=Path,required=True)
    ap.add_argument('--plan',type=Path,required=True)
    ap.add_argument('--foreground',action='store_true');ap.add_argument('--minimum-ms',type=int,default=60000)
    ap.add_argument('--prestart-activity',action='store_true',help='Start only the protected diagnostic Activity before no-restart instrumentation')
    ap.add_argument('--wall-ms',type=int,default=180000);ap.add_argument('--outer-seconds',type=int,default=240)
    ap.add_argument('--iterations',type=int,default=1000);ap.add_argument('--poll-seconds',type=float,default=2)
    ap.add_argument('--stall-seconds',type=int,default=30)
    a=ap.parse_args();out=a.out_dir.resolve();private=a.private_dir.resolve()
    if not out.is_relative_to(ROOT/'artifacts/android-close-investigation-20261030') or out.exists():ap.error('fresh scoped receipt directory required')
    if not private.is_relative_to(ROOT/'build') or private.exists():ap.error('fresh private build directory required')
    if not 0<=a.minimum_ms<a.wall_ms<=1800000 or not 30<=a.outer_seconds<=2100 or not 1<=a.iterations<=100000 or not 1<=a.poll_seconds<=5 or not 10<=a.stall_seconds<=120:ap.error('invalid bounded profile')
    if a.prestart_activity and not a.foreground:ap.error('prestart requires foreground')
    out.mkdir(parents=True);private.mkdir(parents=True)
    receipt={'schema_version':1,'scope':'OWN_ANDROID_CLOSE_FUNCTIONAL_NOT_PERFORMANCE','state':'PREPARING',
      'source_sha256':digest(Path(__file__)),'device_serial_sha256':sha(a.serial.encode()),'started_at_utc':now(),
      'explicit_authorization':'CURRENT_USER_REQUEST_AND_PRIOR_NEW_DEVICE_AUTHORIZATION','shared_device_lock':False,
      'profile':{'foreground_requested':a.foreground,'prestart_activity':a.prestart_activity,'minimum_ms':a.minimum_ms,'wall_ms':a.wall_ms,
                 'outer_seconds':a.outer_seconds,'minimum_iterations':a.iterations,'cgroup_poll_seconds':a.poll_seconds,
                 'stall_seconds':a.stall_seconds,'AMS_injection':False},'gates':[],'commands':[],
      'cgroup_samples':[],'raw_transport':'IGNORED_BUILD_ONLY','BIL':'NOT_MODIFIED','performance':'NOT_MEASURED'}
    def save():
        temp=out/'receipt.pending';data=(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n').encode()
        with temp.open('wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,out/'receipt.json')
    def adb(label,parts,required=True,timeout=20,journal=True):
        try:
            result=subprocess.run([str(a.adb),'-s',a.serial,*parts],capture_output=True,timeout=timeout)
            row={'name':label,'exit_code':result.returncode,'timed_out':False,'stdout_sha256':sha(result.stdout),'stderr_sha256':sha(result.stderr)}
            stdout,stderr=result.stdout,result.stderr
        except subprocess.TimeoutExpired as e:
            stdout,stderr=e.stdout or b'',e.stderr or b'';row={'name':label,'exit_code':None,'timed_out':True,'stdout_sha256':sha(stdout),'stderr_sha256':sha(stderr),'remote_state':'UNKNOWN'}
        if journal or row['timed_out'] or (required and row['exit_code']!=0):receipt['commands'].append(row);save()
        if required and (row['exit_code']!=0 or row['timed_out']):raise RuntimeError(label+'_failed_or_unknown')
        return row,stdout.decode('utf-8','replace')
    def gate(label):
        _,ps=adb(label+'-processes',['shell','ps','-A','-o','PID,NAME,ARGS'])
        entries=[line.split(None,2) for line in ps.splitlines() if line.strip()]
        entries=[entry for entry in entries if len(entry)>=2 and entry[0].isdigit()]
        if not entries:raise RuntimeError('process_inventory_unparseable')
        busy=[entry for entry in entries if BUSY_PROCESS.search(' '.join(entry[1:])) or 'am instrument' in ' '.join(entry[1:])]
        _,activity=adb(label+'-activity',['shell','dumpsys','activity'])
        active=[line for line in activity.splitlines() if 'ActiveInstrumentation{' in line or
                (re.search(r'\bmActiveInstrumentation\s*[:=]',line) and not re.search(r'[:=]\s*(?:null|None|\[\]|\{\}|0)\s*$',line))]
        receipt['gates'].append({'name':label,'parsed_processes':len(entries),'matching_tests':len(busy),'active_instrumentation':len(active),
          'result':'PASS_OBSERVATIONAL_ONLY' if not busy and not active else 'BLOCKED'});save()
        if busy or active:raise RuntimeError('other_device_test_or_instrumentation')
    def certificate(path):
        command=[str(a.java_home/'bin/java.exe'),'-jar',str(a.sdk/'build-tools/37.0.0/lib/apksigner.jar'),'verify','--print-certs',str(path)]
        result=subprocess.run(command,capture_output=True,timeout=30);values=re.findall(rb'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})',result.stdout)
        if result.returncode or len(values)!=1:raise RuntimeError('APK_certificate_check_failed')
        return values[0].decode().lower()
    def progress():
        row,value=adb('own-progress',['shell','run-as',PACKAGE,'cat','cache/mgmem-r8-progress.json'],required=False,timeout=8,journal=False)
        try:return json.loads(value) if row['exit_code']==0 else None
        except ValueError:return None
    def read_own_pid():
        row,value=adb('own-pid',['shell','pidof',PACKAGE],required=False,timeout=8,journal=False)
        ids=value.split();return ids[0] if row['exit_code']==0 and len(ids)==1 and ids[0].isdigit() else None
    def freezer(pid,uid):
        expected=f'/uid_{uid}/pid_{pid}'
        _,raw=adb('own-cgroup',['shell','run-as',PACKAGE,'cat',f'/proc/{pid}/cgroup'],timeout=8,journal=False)
        if '0::'+expected not in raw.splitlines():raise RuntimeError('own_cgroup_identity_unavailable')
        base='/sys/fs/cgroup'+expected
        _,mode=adb('own-cgroup-freeze',['shell','cat',base+'/cgroup.freeze'],timeout=8,journal=False)
        _,events=adb('own-cgroup-events',['shell','cat',base+'/cgroup.events'],timeout=8,journal=False)
        fields=dict(line.split(None,1) for line in events.splitlines() if len(line.split(None,1))==2)
        if mode.strip() not in ('0','1') or fields.get('frozen') not in ('0','1'):raise RuntimeError('freezer_observation_unavailable')
        return {'observed_at_utc':now(),'mode':mode.strip(),'frozen':fields['frozen'],'populated':fields.get('populated'),
                'cgroup_path_sha256':sha(expected.encode())}
    save();child=None;stream_out=None;stream_err=None;started=None;last_progress=None;last_change=None;prestart_initiated=False
    try:
        plan=json.loads(a.plan.read_text(encoding='utf-8'))
        if plan.get('state')!='FROZEN_BEFORE_DEVICE_EXECUTION' or plan['minimum_ms']!=a.minimum_ms or plan['wall_ms']!=a.wall_ms or plan['outer_seconds']!=a.outer_seconds or plan['iterations']!=a.iterations or plan['poll_seconds']!=a.poll_seconds or plan['stall_seconds']!=a.stall_seconds:
            raise RuntimeError('profile_differs_from_frozen_plan')
        if ('foreground' if a.foreground else 'background') not in plan['variants']:raise RuntimeError('variant_not_selected_in_plan')
        if bool(plan.get('foreground_prestart',False) and a.foreground)!=a.prestart_activity:raise RuntimeError('bootstrap_differs_from_frozen_plan')
        if digest(a.build_receipt)!=plan['build_receipt_sha256']:raise RuntimeError('build_receipt_differs_from_frozen_plan')
        for item in plan['files']:
            path=(ROOT/item['path']).resolve()
            if not path.is_relative_to(ROOT) or digest(path)!=item['sha256']:raise RuntimeError('frozen_input_drift:'+item['path'])
        receipt['plan_sha256']=digest(a.plan)
        gate('initial')
        _,api=adb('api',['shell','getprop','ro.build.version.sdk']);_,abi=adb('abi',['shell','getprop','ro.product.cpu.abi'])
        _,page=adb('page',['shell','getconf','PAGESIZE']);_,model=adb('model',['shell','getprop','ro.product.model'])
        api=int(api.strip());abi=abi.strip();page=int(page.strip())
        if api!=33 or abi!='arm64-v8a' or page!=4096:raise RuntimeError('wrong_device_profile')
        receipt['device']={'api':api,'abi':abi,'page_bytes':page,'model':model.strip()}
        built=json.loads(a.build_receipt.read_text(encoding='utf-8'));apk=ROOT/next(i['path'] for i in built['artifacts'] if i['path'].endswith('.apk'))
        if built['status']!='PASS' or built['abi']!=abi or built['package_name']!=PACKAGE:raise RuntimeError('build_receipt_scope')
        for item in built['sources']+built['artifacts']:
            path=ROOT/item['path']
            if not path.resolve().is_relative_to(ROOT) or digest(path)!=item['sha256']:raise RuntimeError('build_or_source_drift')
        if certificate(apk)!=built['certificate_sha256'].lower():raise RuntimeError('new_certificate_mismatch')
        _,badging=adb('boot',['shell','cat','/proc/sys/kernel/random/boot_id']);receipt['boot_id_sha256']=sha(badging.strip().encode())
        _,paths=adb('existing-package',['shell','pm','path',PACKAGE]);matched=re.findall(r'(?m)^package:(/data/app/[A-Za-z0-9_./=+~-]+\.apk)\r?$',paths)
        if len(matched)!=1:raise RuntimeError('expected_one_existing_diagnostic_package')
        installed=private/'existing.apk';adb('pull-own-installed',['pull',matched[0],str(installed)])
        if certificate(installed)!=built['certificate_sha256'].lower():raise RuntimeError('existing_diagnostic_signer_differs')
        if digest(installed)!=digest(apk):
            _,installation=adb('update-same-signer-own-package',['install','-r',str(apk)],timeout=120)
            if 'Success' not in installation:raise RuntimeError('own_install_not_confirmed')
        receipt['APK']={'sha256':digest(apk),'build_receipt_sha256':digest(a.build_receipt),'certificate_sha256':built['certificate_sha256'],'package':PACKAGE}
        previous=progress();previous_id=previous.get('run_id') if previous else None
        _,uid=adb('own-uid',['shell','run-as',PACKAGE,'id','-u']);uid=uid.strip()
        if not uid.isdigit():raise RuntimeError('own_uid_unavailable')
        prestart_pid=None
        if a.prestart_activity:
            gate('before-own-activity');prestart_initiated=True
            _,launched=adb('start-protected-own-activity',['shell','am','start','-W','-n',ACTIVITY],timeout=25)
            if not re.search(r'(?mi)^Status:\s*ok\s*$',launched):raise RuntimeError('own_activity_start_not_confirmed')
            prestart_pid=read_own_pid()
            if not prestart_pid:raise RuntimeError('own_activity_process_unavailable')
            initial=freezer(prestart_pid,uid)
            if initial['mode']!='0' or initial['frozen']!='0':raise RuntimeError('own_activity_already_frozen')
            receipt['prestart']={'state':'ACTIVITY_STARTED_OWN_PID_UNFROZEN','pid_sha256':sha(prestart_pid.encode()),'freezer':initial}
        gate('before-instrumentation')
        stream_out=(private/'instrumentation.stdout').open('wb');stream_err=(private/'instrumentation.stderr').open('wb')
        command=[str(a.adb),'-s',a.serial,'shell','am','instrument']+(['--no-restart'] if a.prestart_activity else [])+['-w','-r','-e','iterations',str(a.iterations),
          '-e','min_duration_ms',str(a.minimum_ms),'-e','max_duration_ms',str(a.wall_ms),'-e','sample_every','200',
          '-e','observe_ams','false','-e','foreground','true' if a.foreground else 'false',
          '-e','foreground_prestarted','true' if a.prestart_activity else 'false',
          '-e','stall_timeout_ms',str(a.stall_seconds*1000),'-e','capture_stack_on_halt','true',COMPONENT]
        child=subprocess.Popen(command,stdout=stream_out,stderr=stream_err);started=time.monotonic();last_change=started
        receipt['state']='RUNNING';receipt['instrumentation_started_at_utc']=now();save();print(json.dumps({'state':'RUNNING','foreground':a.foreground}),flush=True)
        last_value=None;target_pid=None;last_sample=None
        while time.monotonic()-started<a.outer_seconds:
            if child.poll() is not None:break
            current=progress();pid=read_own_pid()
            if current and current.get('run_id')!=previous_id and current.get('diagnostic_revision')==4:
                value=sha(json.dumps(current,sort_keys=True).encode())
                if value!=last_value:last_value=value;last_change=time.monotonic();receipt['last_own_progress']=current
            if pid:
                if prestart_pid and prestart_pid!=pid:raise RuntimeError('instrumentation_restarted_prestarted_process')
                if target_pid and target_pid!=pid:raise RuntimeError('diagnostic_process_restarted')
                target_pid=pid;sample=freezer(pid,uid);t=time.monotonic()
                sample['elapsed_seconds']=t-started;sample['gap_seconds']=None if last_sample is None else t-last_sample;last_sample=t
                receipt['cgroup_samples'].append(sample);receipt['max_cgroup_sample_gap_seconds']=max((s['gap_seconds'] or 0) for s in receipt['cgroup_samples'])
                if sample['mode']=='1' or sample['frozen']=='1':
                    receipt['state']='ENVIRONMENT_FROZEN_INCOMPLETE';receipt['stop_reason']='OWN_CGROUP_FROZEN';save();break
                if (sample['gap_seconds'] or 0)>10:
                    receipt['state']='OBSERVATION_GAP_INCOMPLETE';receipt['stop_reason']='FREEZER_SAMPLE_GAP_OVER_10_SECONDS';save();break
            if last_value and time.monotonic()-last_change>=a.stall_seconds:
                receipt['state']='PROGRESS_STALLED_INCOMPLETE';receipt['stop_reason']='NO_OWN_PROGRESS_WITHIN_BOUND';save();break
            save();time.sleep(a.poll_seconds)
        else:receipt['state']='OUTER_TIMEOUT_INCOMPLETE';receipt['stop_reason']='OUTER_BOUND'
        if child.poll() is None:
            adb('stop-only-own-diagnostic',['shell','am','force-stop',PACKAGE]);receipt['normal_close_proven']=False
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:receipt['remote_process_state']='UNKNOWN';child.terminate()
        stream_out.close();stream_err.close();stdout=(private/'instrumentation.stdout').read_text(encoding='utf-8',errors='replace')
        receipt['instrumentation_exit_code']=child.returncode;receipt['instrumentation_stdout_sha256']=digest(private/'instrumentation.stdout')
        receipt['instrumentation_stderr_sha256']=digest(private/'instrumentation.stderr');receipt['elapsed_seconds']=time.monotonic()-started
        if receipt['state']=='RUNNING':
            if child.returncode!=0:raise RuntimeError('instrumentation_command_failed')
            report=android_checks(stdout,a.minimum_ms//1000,api,built['r8_evidence']['marker_after'])
            if report.get('diagnostic_revision')!=4:raise RuntimeError('wrong_diagnostic_revision')
            if report.get('iterations_completed',0)<a.iterations:raise RuntimeError('minimum_completed_iterations_not_met')
            if report.get('foreground_requested') is not a.foreground:raise RuntimeError('foreground_profile_report_mismatch')
            if bool(report.get('foreground_prestarted',False)) is not a.prestart_activity:raise RuntimeError('prestarted_profile_report_mismatch')
            if a.foreground and report.get('foreground_resumed') is not True:raise RuntimeError('foreground_not_observed_resumed')
            if a.foreground and report.get('foreground_finished') is not True:raise RuntimeError('foreground_not_cleanly_finished')
            if a.foreground and report.get('foreground_pause_count_before_finish')!=0:raise RuntimeError('foreground_paused_during_work')
            if report.get('fire_and_forget_state')!='PASS_REGISTRATION_REMOVED_WITHOUT_CALLER_POLL':raise RuntimeError('no_poll_cleanup_not_verified')
            if not receipt['cgroup_samples'] or any(s['mode']!='0' or s['frozen']!='0' for s in receipt['cgroup_samples']):raise RuntimeError('no_valid_freezer_observation')
            if receipt['cgroup_samples'][0]['elapsed_seconds']>10 or (last_sample is not None and time.monotonic()-last_sample>10):raise RuntimeError('freezer_observation_start_or_end_gap')
            reported_pid=report.pop('process_pid',None)
            if not target_pid or reported_pid!=int(target_pid):raise RuntimeError('report_pid_differs_from_observed_process')
            report['process_pid_sha256']=sha(str(reported_pid).encode())
            receipt['android_report']=report;receipt['state']='PASS_OBSERVED_UNFROZEN_SAMPLES';receipt['normal_close_proven']=True
        gate('after')
        _,boot=adb('boot-after',['shell','cat','/proc/sys/kernel/random/boot_id'])
        if sha(boot.strip().encode())!=receipt['boot_id_sha256']:raise RuntimeError('device_rebooted')
    except Exception as error:
        receipt.update(state='FAILED_OR_BLOCKED',reason=str(error),normal_close_proven=False)
        # An active child is owned by this run; isolate it without touching other apps.
        if child is not None and child.poll() is None:
            try:adb('stop-own-on-error',['shell','am','force-stop',PACKAGE]);child.wait(timeout=15)
            except Exception:receipt['remote_process_state']='UNKNOWN'
        elif prestart_initiated and child is None:
            try:adb('stop-own-activity-on-error',['shell','am','force-stop',PACKAGE])
            except Exception:receipt['remote_process_state']='UNKNOWN'
    finally:
        if stream_out and not stream_out.closed:stream_out.close()
        if stream_err and not stream_err.closed:stream_err.close()
        receipt['finished_at_utc']=now();save()
    print(json.dumps({'state':receipt['state'],'foreground':a.foreground,'samples':len(receipt['cgroup_samples']),'reason':receipt.get('reason')}),flush=True)
    raise SystemExit(0 if receipt['state']=='PASS_OBSERVED_UNFROZEN_SAMPLES' else 2)
if __name__=='__main__':main()
