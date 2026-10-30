"""Test the final sanitized R8 APK only on this task's isolated API34 AVD.

The earlier guard is preserved and is not repeated. Raw inventories, installed
APK copies and command output remain inside ignored build/. No global settings,
app-data clearing, adb restart, or other device/process operation occurs.
"""
from pathlib import Path
import hashlib,json,os,re,subprocess,time
from datetime import datetime,timezone
import verify_api34_retry as common

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'artifacts/android-close-investigation-20261030'
OWN=ROOT/'build/android-close-api34-avd'
PRIVATE=OWN/'private-distribution-final'
BUILD=BASE/'final-r8-x86_64-build.json'
AAR_BUILD=BASE/'final-arm64-aar-build.json'
OUT=BASE/'api34-distribution-smoke'/'receipt.json'
PACKAGE=common.PACKAGE

def main():
    common.require(re.fullmatch(r'emulator-[0-9]{4,5}',common.SERIAL),'explicit_owned_emulator_serial_required')
    port=common.SERIAL.split('-',1)[1] # Numeric-only, validated above before shell construction.
    if PRIVATE.exists() or OUT.exists():raise RuntimeError('fresh_private_and_public_output_required')
    PRIVATE.mkdir(parents=True);common.PRIVATE=PRIVATE;common.BUILD=BUILD;common.NATIVE_BUILD=AAR_BUILD
    data={'state':'PREPARING','scope':'OWN_REUSED_API34_AVD_FINAL_SANITIZED_R8_SHORT_FUNCTIONAL',
          'started_at_utc':common.utc(),'runner_sha256':common.filehash(Path(__file__)),
          'helper_sha256':common.filehash(ROOT/'tools/verify_api34_retry.py'),
          'device_serial_sha256':common.sha(common.SERIAL.encode()),'gates':[],
          'guard':'NOT_REPEATED_PREVIOUS_INDEPENDENT_EVIDENCE','performance':'NOT_MEASURED','BIL':'NOT_INTEGRATED',
          'shared_device_lock':False,'raw_logs':'IGNORED_BUILD_ONLY'}
    common.save(OUT,data)
    child=None;rawout=None;rawerr=None
    try:
        pid_text=(OWN/'pid-distribution.txt').read_text().strip();common.require(pid_text.isdigit(),'own_host_pid_invalid')
        # PID is digits from the task-owned file, never externally supplied shell text.
        script=("$p=Get-CimInstance Win32_Process -Filter 'ProcessId="+pid_text+"'; "
                "if($p -and $p.Name -eq 'emulator.exe' -and $p.CommandLine -match '-avd MemoryRetryApi34' "
                "-and $p.CommandLine -match '-port "+port+"(?:\\s|$)'){'MATCH'}else{'NO_MATCH'}")
        host=subprocess.run(['powershell.exe','-NoProfile','-Command',script],capture_output=True,text=True,timeout=15)
        common.require(host.returncode==0 and host.stdout.strip()=='MATCH','own_host_AVD_process_identity_unknown')
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            _,boot=common.command('boot-'+str(time.monotonic_ns()),['shell','getprop','sys.boot_completed'],timeout=8,required=False)
            if boot.strip()=='1':break
            time.sleep(3)
        else:raise RuntimeError('own_emulator_boot_timeout')
        _,avd=common.command('avd-name',['emu','avd','name']);common.require(avd.splitlines()[0].strip()=='MemoryRetryApi34','own_AVD_identity_mismatch')
        data['gates'].append(common.occupancy('before-update'))
        metadata={}
        for key,command in {'api':['shell','getprop','ro.build.version.sdk'],'abi':['shell','getprop','ro.product.cpu.abi'],
                            'page_bytes':['shell','getconf','PAGESIZE'],'boot':['shell','cat','/proc/sys/kernel/random/boot_id']}.items():
            _,value=common.command('device-'+key,command);metadata[key]=value.strip()
        common.require(metadata['api']=='34' and metadata['abi']=='x86_64' and metadata['page_bytes']=='4096','unexpected_device_profile')
        data['device']={'api':34,'abi':'x86_64','page_bytes':4096,'boot_sha256':common.sha(metadata['boot'].encode())}
        built,apk,data['APK']=common.build_identity()
        old_progress=common.read_cache('mgmem-r8-progress.json');old_id=old_progress.get('run_id') if old_progress else None
        p,path=common.command('installed-package',['shell','pm','path',PACKAGE],required=False)
        paths=re.findall(r'(?m)^package:(/data/app/[A-Za-z0-9_./=+~-]+\.apk)\r?$',path)
        common.require(p['exit_code']==0 and len(paths)==1,'own_previously_installed_APK_missing_or_ambiguous')
        original=PRIVATE/'prior-own-app.apk';common.command('pull-prior-own-app',['pull',paths[0],str(original)])
        signer=[str(common.JAVA),'-jar',str(common.SDK/'build-tools/37.0.0/lib/apksigner.jar'),'verify','--print-certs']
        def cert(path):
            result=subprocess.run(signer+[str(path)],capture_output=True,text=True,timeout=30,check=True)
            found=re.findall(r'certificate SHA-256 digest:\s*([a-fA-F0-9]{64})',result.stdout)
            common.require(len(found)==1,'single_signer_required');return found[0].lower()
        old_cert,new_cert=cert(original),cert(apk)
        common.require(old_cert==new_cert==built['certificate_sha256'],'own_existing_certificate_mismatch')
        data['prior_own_APK']={'sha256':common.filehash(original),'certificate_sha256':old_cert,'data_cleared':False}
        if common.filehash(original)!=common.filehash(apk):
            _,result=common.command('update-own-same-certificate-APK',['install','-r',str(apk)],timeout=120)
            common.require('Success' in result,'own_APK_update_failed');data['own_APK_update']='SAME_CERTIFICATE_UPDATE_NO_DATA_CLEAR'
        else:data['own_APK_update']='ALREADY_IDENTICAL_NO_INSTALL'
        _,path=common.command('updated-package-path',['shell','pm','path',PACKAGE]);paths=re.findall(r'(?m)^package:(/data/app/[A-Za-z0-9_./=+~-]+\.apk)\r?$',path)
        common.require(len(paths)==1,'updated_APK_path_ambiguous');actual=PRIVATE/'updated-own-app.apk'
        common.command('pull-updated-own-app',['pull',paths[0],str(actual)])
        common.require(common.filehash(actual)==common.filehash(apk) and cert(actual)==new_cert,'installed_final_APK_identity_mismatch')
        data['gates'].append(common.occupancy('immediately-before-instrumentation'));data['state']='RUNNING';common.save(OUT,data)
        rawout=(PRIVATE/'instrumentation.stdout').open('wb');rawerr=(PRIVATE/'instrumentation.stderr').open('wb')
        launch=[str(common.ADB),'-s',common.SERIAL,'shell','am','instrument','-w','-r','-e','iterations','20',
                '-e','min_duration_ms','8000','-e','max_duration_ms','60000','-e','sample_every','20',
                '-e','observe_ams','false','-e','stall_timeout_ms','10000',common.COMPONENT]
        child=subprocess.Popen(launch,stdout=rawout,stderr=rawerr);start=time.monotonic();samples=[];last=0
        while child.poll() is None and time.monotonic()-start<75:
            if len(samples)<3 and time.monotonic()-last>=2:
                _,pids=common.command('own-pid-'+str(time.monotonic_ns()),['shell','pidof',PACKAGE],timeout=8,required=False)
                values=pids.split()
                if len(values)==1 and values[0].isdigit():samples.append(common.cgroup_sample(values[0]));last=time.monotonic()
            time.sleep(.25)
        if child.poll() is None:
            data.update(state='INCOMPLETE_TRANSPORT_TIMEOUT_REMOTE_STATE_UNKNOWN',cgroup_samples=samples);common.save(OUT,data)
            raise RuntimeError('transport_timeout_do_not_kill')
        rawout.close();rawerr.close();rawout=rawerr=None
        stdout=(PRIVATE/'instrumentation.stdout').read_text(encoding='utf-8',errors='replace')
        stderr=(PRIVATE/'instrumentation.stderr').read_text(encoding='utf-8',errors='replace')
        reports=re.findall(r'(?m)^INSTRUMENTATION_RESULT: report_json=(\{.*\})\r?$',stdout)
        common.require(child.returncode==0 and len(reports)==1 and not stderr.strip(),'instrumentation_transport_failed')
        report=json.loads(reports[0]);common.require(report.get('status')=='PASS' and re.search(r'(?m)^INSTRUMENTATION_CODE: 0\s*$',stdout),'functional_report_not_PASS')
        common.require(report['api']==34 and report['diagnostic_revision']==4 and report['r8_marker_runtime_name']==built['r8_evidence']['marker_after'],'runtime_identity_mismatch')
        common.require(report['run_id']!=old_id,'stale_run_id')
        common.require(report['iterations_completed']>=20 and report['actual_duration_ms']>=8000,'insufficient_completed_work')
        common.require(report['logical_unavailable_reason_checks']=='PASS','logical_unavailable_reasons_not_verified')
        common.require(report['fire_and_forget_state']=='PASS_REGISTRATION_REMOVED_WITHOUT_CALLER_POLL','fire_and_forget_not_proven')
        common.require(report['final_backing_bytes_each_domain']==0 and not report['inject_progress_lock_timeout'] and not report['inject_main_running_timeout'],'close_zero_or_injection_failure')
        common.require(report['actual_low_memory_event'] is False and report['performance_qualified'] is False and report['BIL_integrated'] is False,'qualification_scope_failure')
        common.require(any(s.get('state')=='OBSERVED' for s in samples),'own_cgroup_unavailable')
        common.require(all(s.get('cgroup_freeze',0)==0 and s.get('cgroup_events_frozen',0)==0 for s in samples),'own_process_freeze_observed')
        run_hash=common.sha(report['run_id'].encode());report.pop('run_id',None);report.pop('process_pid',None)
        data.update(state='PASS_SCOPED_API34_FINAL_SANITIZED_R8_FUNCTIONAL',instrumentation_report=report,
            own_run_id_sha256=run_hash,previous_run_id_distinct=True,cgroup_samples=samples,
            cgroup_scope='OWN_PID_DISCRETE_SAMPLES_NOT_CONTINUOUS',instrumentation_stdout_sha256=common.filehash(PRIVATE/'instrumentation.stdout'),
            instrumentation_stderr_sha256=common.filehash(PRIVATE/'instrumentation.stderr'),finished_at_utc=common.utc())
        data['gates'].append(common.occupancy('after-instrumentation'));common.save(OUT,data)
        print(json.dumps({'state':data['state'],'iterations':report['iterations_completed'],
            'duration_ms':report['actual_duration_ms'],'reason_checks':report['logical_unavailable_reason_checks'],
            'fire_and_forget':report['fire_and_forget_state'],'cgroup_observations':len(samples)}),flush=True)
    except Exception as error:
        if rawout:rawout.close()
        if rawerr:rawerr.close()
        data.update(state='BLOCKED_OR_INCOMPLETE',reason=str(error),finished_at_utc=common.utc(),
                    remote_state='UNKNOWN_IF_INSTRUMENTATION_STILL_ACTIVE_NO_STOP_REQUESTED')
        common.save(OUT,data);print(json.dumps({'state':data['state'],'reason':str(error)}),flush=True);raise

if __name__=='__main__':main()
