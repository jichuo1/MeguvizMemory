"""Read-only API33 postflight for the exact completed Android close run.

Pulls only this diagnostic APK into a new ignored build directory. Inventories
and complete command outputs remain private there; public JSON contains hashes
and filtered counts only. No device process, data, APK or policy is modified.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import zipfile
from run_arm64_validation import BUSY_PROCESS
from run_android_close_round import ReceiptPublicationError, safe_reason, write_receipt_atomic

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'artifacts/android-close-investigation-20261030'
PACKAGE='io.github.jichuo1.meguviz.memory.pressureprobe.r8'
ACTIVITY='PressureR8ForegroundActivity'
EXPECTED_CERT='9615607b900dffe1a6bce5820b9b222d8cb4c3a160e72186d261c4b26c172aa3'
SHA=re.compile(r'^[0-9a-f]{64}$')
EXTRA_BUSY=re.compile(r'\bam\s+instrument\b|androidx\.test|uiautomator|simpleperf|\bmonkey\b',re.I)

class Blocked(RuntimeError): pass
def require(condition,reason):
    if not condition:raise Blocked(reason)
def digest(data):return hashlib.sha256(data).hexdigest()
def filehash(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()
def load(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def finite(value):return type(value) in (int,float) and math.isfinite(value)

def verify_long(value,plan,plan_path,build_sha,apk_sha,cert):
    require(value.get('state')=='PASS_OBSERVED_UNFROZEN_SAMPLES' and value.get('normal_close_proven') is True,'long_round_not_completed')
    require(value.get('plan_sha256')==filehash(plan_path),'long_plan_identity_mismatch')
    require(plan.get('state')=='FROZEN_BEFORE_DEVICE_EXECUTION' and plan.get('build_receipt_sha256')==build_sha and plan.get('APK_sha256')==apk_sha,'long_plan_artifact_mismatch')
    apk=value.get('APK',{});require(apk.get('package')==PACKAGE and apk.get('sha256')==apk_sha and apk.get('build_receipt_sha256')==build_sha and apk.get('certificate_sha256')==cert,'long_APK_identity_mismatch')
    report=value.get('android_report',{});profile=value.get('profile',{})
    require(type(plan.get('minimum_ms')) is int and plan['minimum_ms']>=900000 and type(plan.get('iterations')) is int and plan['iterations']>=1000,'long_minimum_invalid')
    require(profile.get('minimum_ms')==plan['minimum_ms'] and profile.get('minimum_iterations')==plan['iterations'],'long_profile_mismatch')
    require(report.get('status')=='PASS' and type(report.get('actual_duration_ms')) is int and report['actual_duration_ms']>=plan['minimum_ms'] and type(report.get('iterations_completed')) is int and report['iterations_completed']>=plan['iterations'],'long_report_incomplete')
    require(report.get('final_backing_bytes_each_domain')==0 and report.get('fire_and_forget_state')=='PASS_REGISTRATION_REMOVED_WITHOUT_CALLER_POLL' and report.get('logical_unavailable_reason_checks')=='PASS','long_close_not_proven')
    require(report.get('foreground_requested') is True and report.get('foreground_prestarted') is True and report.get('foreground_resumed') is True and report.get('foreground_finished') is True and report.get('foreground_pause_count_before_finish')==0,'long_foreground_not_proven')
    require(report.get('performance_qualified') is False and report.get('BIL_integrated') is False and report.get('actual_low_memory_event') is False,'long_scope_invalid')
    samples=value.get('cgroup_samples');require(isinstance(samples,list) and len(samples)>=2,'long_freezer_samples_missing')
    require(all(isinstance(s,dict) and s.get('mode')=='0' and s.get('frozen')=='0' for s in samples),'long_frozen_or_unknown')
    identities={s.get('cgroup_path_sha256') for s in samples};require(len(identities)==1 and SHA.fullmatch(str(next(iter(identities)))),'long_cgroup_identity_changed')
    require(finite(samples[0].get('elapsed_seconds')) and 0<=samples[0]['elapsed_seconds']<=10,'long_first_sample_gap')
    require(all(finite(s.get('gap_seconds')) and 0<=s['gap_seconds']<=10 for s in samples[1:]),'long_sample_gap')
    require(finite(samples[-1].get('elapsed_seconds')) and samples[-1]['elapsed_seconds']>=max(0,report['actual_duration_ms']/1000-10),'long_end_sample_gap')
    gates={g.get('name') for g in value.get('gates',[]) if isinstance(g,dict) and g.get('result')=='PASS_OBSERVATIONAL_ONLY'}
    require({'initial','before-instrumentation','after'}.issubset(gates),'long_device_gates_missing')
    require(value.get('instrumentation_exit_code')==0,'long_instrumentation_incomplete')
    require(SHA.fullmatch(str(value.get('device_serial_sha256'))) and SHA.fullmatch(str(value.get('boot_id_sha256'))),'long_device_identity_missing')
    require(report.get('process_pid_sha256')==value.get('prestart',{}).get('pid_sha256') and SHA.fullmatch(str(report.get('process_pid_sha256'))),'long_prestart_pid_mismatch')

def process_inventory(text):
    rows=[];matches=0;own=0
    for line in text.splitlines():
        fields=line.split(None,2)
        if len(fields)<2 or not fields[0].isdigit():continue
        name=fields[1];arguments=fields[2] if len(fields)==3 else name
        rows.append(1)
        if name==PACKAGE or arguments==PACKAGE:own+=1
        if BUSY_PROCESS.search(name+' '+arguments) or EXTRA_BUSY.search(name+' '+arguments):matches+=1
    require(len(rows)>0,'process_inventory_unparseable')
    return len(rows),matches,own

def activity_inventory(text):
    active=0;unknown=0
    for line in text.splitlines():
        if 'ActiveInstrumentation' not in line:continue
        if 'ActiveInstrumentation{' in line:active+=1;continue
        marker=re.search(r'\bmActiveInstrumentation\s*[:=]\s*(.*)$',line)
        if not marker or marker.group(1).strip() not in ('null','None','[]','{}','0'):unknown+=1
    require(not unknown,'instrumentation_inventory_unknown')
    return active

def own_resumed(text):
    markers=[]
    for line in text.splitlines():
        match=re.search(r'\b(?:mResumedActivity|m?topResumedActivity|Resumed)\s*[:=]\s*(.*)$',line,re.I)
        if not match:continue
        value=match.group(1).strip()
        if value.lower() in ('null','none','[]','{}'):markers.append(False)
        elif 'ActivityRecord{' in value:
            markers.append(PACKAGE in value and ACTIVITY in value)
        else:return 'UNKNOWN'
    if not markers:return 'UNKNOWN'
    return any(markers)

class Postflight:
    def __init__(self,args):
        self.args=args;self.start=time.monotonic();self.counter=0
        self.output=args.output.resolve();require(self.output.is_relative_to(BASE.resolve()) and not self.output.exists(),'fresh_scoped_output_required')
        self.private=ROOT/'build'/('android-close-postflight-'+secrets.token_hex(8))
        require(self.private.resolve().is_relative_to((ROOT/'build').resolve()),'private_directory_outside_build')
        self.private.mkdir(parents=True,exist_ok=False)
        self.value={'state':'RUNNING','scope':'READ_ONLY_API33_INSTALLED_BYTES_AND_POSTFLIGHT','source_sha256':filehash(Path(__file__)),
                    'atomic_helper_sha256':filehash(ROOT/'tools/run_android_close_round.py'),
                    'device_serial_sha256':digest(args.serial.encode()),'long_receipt_sha256':None,'build_receipt_sha256':None,
                    'installed_APK_sha256':None,'certificate_sha256':None,'jni_member_sha256':None,'core_member_sha256':None,
                    'boot_id_sha256':None,'before_gate':None,'after_gate':None,'own_activity_resumed':'UNKNOWN',
                    'protocol_and_lifecycle_pass':False,'installed_byte_identity_verified':False,
                    'commands':[],'shared_device_lock':False,'raw_APK_and_logs':'PRIVATE_BUILD_DIRECTORY_ONLY',
                    'uninstall_performed':False,'data_cleared':False,'process_stopped':False,'global_policy_modified':False,
                    'qualification':'OBSERVATIONAL_POSTFLIGHT_NOT_PERFORMANCE_OR_FORMAL_RELEASE'}
        self.save()
    def save(self):
        self.output.parent.mkdir(parents=True,exist_ok=True)
        write_receipt_atomic(self.output,self.value)
    def command(self,label,args,device=True,timeout=20):
        remaining=self.args.overall_timeout-(time.monotonic()-self.start)
        require(remaining>0,'overall_deadline_exceeded');self.counter+=1
        command=([str(self.args.adb),'-s',self.args.serial] if device else [])+[str(x) for x in args]
        try:
            result=subprocess.run(command,capture_output=True,timeout=min(timeout,remaining));out,err=result.stdout,result.stderr
            row={'name':label,'exit_code':result.returncode,'timed_out':False}
        except subprocess.TimeoutExpired as error:
            out,err=error.stdout or b'',error.stderr or b'';row={'name':label,'exit_code':None,'timed_out':True,'remote_state':'UNKNOWN'}
        (self.private/(label+'.stdout')).write_bytes(out);(self.private/(label+'.stderr')).write_bytes(err)
        row.update(stdout_sha256=digest(out),stderr_sha256=digest(err));self.value['commands'].append(row);self.save()
        require(not row['timed_out'],label+'_timeout');require(row['exit_code']==0,label+'_failed')
        output=out.decode('utf-8','replace');error=err.decode('utf-8','replace')
        require('Permission Denial' not in output and 'Permission denied' not in error and not re.search(r'(?im)^\s*(Unknown command|Error:|Error dumping service|Can.t find service)',output+'\n'+error),label+'_unsupported_or_denied')
        return output
    def gate(self,name):
        ps=self.command(name+'-processes',['shell','ps','-A','-o','PID,NAME,ARGS'])
        row_count,matches,own=process_inventory(ps)
        activities=self.command(name+'-activity',['shell','dumpsys','activity'])
        active=activity_inventory(activities)
        result={'result':'PASS_OBSERVATIONAL_ONLY' if matches==0 and active==0 else 'BLOCKED',
                'process_rows':row_count,'matching_tests':matches,'own_processes':own,'active_instrumentation':active,
                'process_inventory_sha256':digest(ps.encode()),'activity_inventory_sha256':digest(activities.encode())}
        self.value[name+'_gate']=result;self.save()
        require(result['result']=='PASS_OBSERVATIONAL_ONLY',name+'_other_test_or_instrumentation')
        return activities
    def run(self):
        try:
            require(re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',self.args.serial) is not None,'invalid_serial_argument')
            require(self.args.adb.is_file() and self.args.adb.name.lower() in ('adb','adb.exe'),'adb_path_invalid')
            long_path=self.args.long_receipt.resolve();plan_path=self.args.long_plan.resolve();build_path=BASE/'final-r8-arm64-build.json'
            require(long_path.is_relative_to(BASE.resolve()) and plan_path.is_relative_to(BASE.resolve()) and long_path.is_file() and plan_path.is_file(),'long_inputs_outside_stage_or_missing')
            long=load(long_path);plan=load(plan_path);built=load(build_path)
            self.value['long_receipt_sha256']=filehash(long_path);self.value['build_receipt_sha256']=filehash(build_path)
            require(built.get('status')=='PASS' and built.get('abi')=='arm64-v8a' and built.get('package_name')==PACKAGE,'wrong_build_receipt')
            require(built.get('certificate_sha256')==EXPECTED_CERT,'build_certificate_mismatch')
            require(re.fullmatch(r'[0-9]+(?:\.[0-9]+)*',built.get('build_tools','')) is not None,'invalid_build_tools_version')
            apks=[x for x in built.get('artifacts',[]) if x.get('path','').endswith('.apk')]
            require(len(apks)==1,'one_built_APK_required')
            built_apk=(ROOT/apks[0]['path']).resolve();aar=(ROOT/built['input_aar']['path']).resolve()
            for path,item in ((built_apk,apks[0]),(aar,built['input_aar'])):
                require(path.is_relative_to(ROOT) and path.is_file() and filehash(path)==item['sha256'],'built_APK_or_AAR_drift')
            verify_long(long,plan,plan_path,self.value['build_receipt_sha256'],filehash(built_apk),EXPECTED_CERT)
            self.value['protocol_and_lifecycle_pass']=True;self.save()
            require(self.value['device_serial_sha256']==long['device_serial_sha256'],'device_serial_mismatch')
            self.gate('before')
            device={}
            for key,command in {'api':['shell','getprop','ro.build.version.sdk'],'abi':['shell','getprop','ro.product.cpu.abi'],
                                'model':['shell','getprop','ro.product.model'],'page_bytes':['shell','getconf','PAGESIZE']}.items():
                value=self.command('device-'+key,command).strip()
                if key in ('api','page_bytes'):
                    require(value.isdigit(),'invalid_device_'+key);value=int(value)
                device[key]=value
            require(device==long.get('device') and device['api']==33 and device['abi']=='arm64-v8a','device_identity_mismatch')
            self.value['device']=device;self.save()
            def boot(label):
                raw=self.command(label,['shell','cat','/proc/sys/kernel/random/boot_id']).strip()
                require(re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',raw) is not None,'boot_id_unparseable')
                return digest(raw.encode())
            before_boot=boot('boot-before');require(before_boot==long['boot_id_sha256'],'boot_id_changed')
            self.value['boot_id_sha256']=before_boot;self.save()
            def package_path(label):
                text=self.command(label,['shell','pm','path',PACKAGE])
                values=re.findall(r'(?m)^package:(/data/app/[A-Za-z0-9_./=+~-]+\.apk)\r?$',text)
                require(len(values)==1 and len([line for line in text.splitlines() if line.strip()])==1,'installed_APK_path_ambiguous')
                return values[0]
            path=package_path('installed-package-path-before');installed=self.private/'installed-own.apk'
            self.command('device-hash-installed',['shell','sha256sum',path]);device_hash=(self.private/'device-hash-installed.stdout').read_text().split()[0]
            self.command('pull-installed-own-APK',['pull',path,str(installed)],timeout=90)
            actual=filehash(installed);require(SHA.fullmatch(device_hash) and device_hash==actual==filehash(built_apk),'installed_APK_bytes_mismatch')
            self.value['installed_APK_sha256']=actual;self.save()
            java=self.args.java_home/'bin'/('java.exe' if os.name=='nt' else 'java')
            tools=self.args.sdk/'build-tools'/built['build_tools'];signer=tools/'lib/apksigner.jar';aapt=tools/('aapt2.exe' if os.name=='nt' else 'aapt2')
            require(java.is_file() and signer.is_file() and aapt.is_file(),'certificate_or_package_tool_missing')
            signature=self.command('verify-installed-certificate',[java,'-jar',signer,'verify','--print-certs',installed],device=False,timeout=60)
            certificates=re.findall(r'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})',signature)
            require(len(certificates)==1 and certificates[0].lower()==EXPECTED_CERT,'installed_certificate_mismatch')
            self.value['certificate_sha256']=certificates[0].lower();self.save()
            badging=self.command('verify-installed-package',[aapt,'dump','badging',installed],device=False)
            require(re.findall(r"(?m)^package: name='([^']+)'",badging)==[PACKAGE],'installed_package_mismatch')
            with zipfile.ZipFile(aar) as package,zipfile.ZipFile(installed) as apk:
                require(package.testzip() is None and apk.testzip() is None,'APK_or_AAR_corrupt')
                require(len(package.namelist())==len(set(package.namelist())) and len(apk.namelist())==len(set(apk.namelist())),'duplicate_archive_members')
                expected=['lib/arm64-v8a/libmgmem_pressure_jni.so','lib/arm64-v8a/libmgmem.so']
                require(sorted(n for n in apk.namelist() if n.endswith('.so'))==sorted(expected),'installed_native_closure_mismatch')
                for name,key in [('libmgmem_pressure_jni.so','jni_member_sha256'),('libmgmem.so','core_member_sha256')]:
                    apk_member='lib/arm64-v8a/'+name;aar_member='jni/arm64-v8a/'+name
                    a,b=package.read(aar_member),apk.read(apk_member)
                    require(a==b,'installed_native_member_mismatch')
                    entries=[item for item in built.get('aar_members',[]) if item.get('member')==aar_member]
                    require(len(entries)==1 and entries[0].get('sha256')==digest(a),'build_native_member_mismatch')
                    self.value[key]=digest(b)
            self.save()
            require(package_path('installed-package-path-after')==path,'installed_package_replaced_during_readback')
            require(boot('boot-after')==before_boot,'boot_changed_during_readback')
            self.value['installed_byte_identity_verified']=True;self.save()
            activities=self.gate('after')
            self.value['own_activity_resumed']=own_resumed(activities);self.save()
            require(self.value['own_activity_resumed'] is not True,'own_activity_still_resumed')
            require(self.value['own_activity_resumed'] is False,'own_activity_resumed_unknown')
            self.value.update(state='PASS_INSTALLED_BYTE_IDENTITY_AND_POSTFLIGHT',finished_at_utc=datetime.now(timezone.utc).isoformat())
        except Blocked as error:
            self.value.update(state='BLOCKED',reason=str(error),finished_at_utc=datetime.now(timezone.utc).isoformat())
        except Exception as error:
            self.value.update(state='BLOCKED',reason='UNEXPECTED_'+type(error).__name__,finished_at_utc=datetime.now(timezone.utc).isoformat())
        try:self.save()
        except ReceiptPublicationError as error:
            self.value.update(state='BLOCKED',reason=safe_reason(error))
            marker=self.output.with_name(self.output.stem+'-publication-failure.json')
            try:write_receipt_atomic(marker,{'state':'BLOCKED','reason':self.value['reason'],
                'long_receipt_sha256':self.value['long_receipt_sha256'],'build_receipt_sha256':self.value['build_receipt_sha256'],
                'pending_retained':self.output.with_suffix('.pending').is_file(),
                'scope':'INCOMPLETE_POSTFLIGHT_NO_DEVICE_MUTATION'})
            except (OSError,ReceiptPublicationError):pass
        print(json.dumps({'state':self.value['state'],'reason':self.value.get('reason'),'own_activity_resumed':self.value['own_activity_resumed']}))
        return 0 if self.value['state']=='PASS_INSTALLED_BYTE_IDENTITY_AND_POSTFLIGHT' else 2

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb',type=Path,required=True);parser.add_argument('--serial',required=True)
    parser.add_argument('--sdk',type=Path,required=True);parser.add_argument('--java-home',type=Path,required=True)
    parser.add_argument('--long-receipt',type=Path,default=BASE/'distribution-long/receipt.json')
    parser.add_argument('--long-plan',type=Path,default=BASE/'distribution-long-plan.json')
    parser.add_argument('--output',type=Path,default=BASE/'distribution-postflight.json')
    parser.add_argument('--overall-timeout',type=int,default=180)
    args=parser.parse_args()
    if not 10<=args.overall_timeout<=300:parser.error('overall timeout must be 10..300 seconds')
    try:status=Postflight(args).run()
    except ReceiptPublicationError as error:
        print(json.dumps({'state':'BLOCKED','reason':safe_reason(error),'scope':'INCOMPLETE_POSTFLIGHT_NO_DEVICE_MUTATION'}));status=2
    raise SystemExit(status)

if __name__=='__main__':main()
