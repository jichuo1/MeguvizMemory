#!/usr/bin/env python3
"""Isolated long-running Android stability diagnostic; plans and logs are private."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import re
import secrets
import subprocess
import sys
import time
import zipfile
from pathlib import Path
import android_managed_round as common
import build_android_managed_stability_probe as builder
import build_android_managed_probe as base

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = builder.PACKAGE
ACTIVITY = builder.ACTIVITY
INSTRUMENT = builder.INSTRUMENT
COMPONENT = PACKAGE + '/' + INSTRUMENT
ACTIVITY_COMPONENT = PACKAGE + '/' + ACTIVITY
SUITE = 'android-managed-stability-v1'
Blocked = common.Blocked
require = common.require
sha = common.sha
digest = common.digest
unique_json = common.unique_json
atomic_receipt = common.atomic_receipt
workspace_file = common.workspace_file
utc = common.utc
SERIAL = common.SERIAL
SHA = common.SHA
UUID = common.UUID
BUSY = common.BUSY
FIELDS = {'suite','status','run_id','pid','sequence','phase','iteration','elapsed_ms',
          'workload_elapsed_ms','snapshot_consistency','foreground_confirmed','reason',
          'pressure_origin','performance_qualified'}


def parse_resumed(text):
    require(not re.search(r'(?i)permission\s+denial|permission\s+denied|can.t find service|error dumping', text), 'activity_permission_or_dump_error')
    values=[]
    for line in text.splitlines():
        match=re.search(r'\b(?:mResumedActivity|m?topResumedActivity|Resumed)\s*[:=]\s*(.*)$',line,re.I)
        if match:
            value=match.group(1).strip()
            require(re.search(r'ActivityRecord\{[^}]+\}',value) is not None,'resumed_unparseable')
            match_component=re.search(r'\b([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)',value)
            require(match_component is not None,'resumed_component_unparseable')
            values.append(match_component.group(1))
    require(values and len(set(values))==1,'resumed_missing_or_ambiguous')
    return values[0] == ACTIVITY_COMPONENT


def parse_progress(data,run_id,pid):
    try:
        value=unique_json(data)
    except (ValueError,TypeError) as error: raise Blocked('progress_invalid_json') from error
    require(isinstance(value,dict) and set(value)==FIELDS,'progress_fields_invalid')
    require(value['suite']==SUITE and value['status'] in ('RUNNING','PASS','FAIL','INCOMPLETE') and
            value['run_id']==run_id and type(value['pid']) is int and value['pid']==pid and
            all(type(value[k]) is int and 0<=value[k]<=2**63-1 for k in ('sequence','iteration','elapsed_ms','workload_elapsed_ms')) and
            value['workload_elapsed_ms']<=value['elapsed_ms'] and
            value['snapshot_consistency']=='non_atomic' and type(value['foreground_confirmed']) is bool and
            value['pressure_origin']=='synthetic' and value['performance_qualified'] is False and
            isinstance(value['phase'],str) and re.fullmatch(r'[a-z_]{1,80}',value['phase']) is not None and
            value['reason'] in ('none','operation_failed','identity_or_timeout','preparation_timeout','duration_or_round_limit',
                                'phase_timeout_unknown','foreground_cleanup_unknown'),
            'progress_scope_or_identity_invalid')
    return value


def classify_observation(progress,stall,run_id,pid,duration_ms):
    try:
        if stall is not None:
            value=parse_progress(json.dumps(stall),run_id,pid)
            require(value['status']=='INCOMPLETE','stall_not_incomplete')
            return 'INCOMPLETE'
        if progress is None:return 'INCOMPLETE'
        value=parse_progress(json.dumps(progress),run_id,pid)
        if value['status']=='PASS':
            require(duration_ms>=900000 and value['phase']=='complete' and value['iteration']>0 and
                    value['workload_elapsed_ms']>=duration_ms and value['foreground_confirmed'] is True and
                    value['reason']=='none','pass_qualification_missing')
        return value['status']
    except (Blocked,ValueError,TypeError):return 'INCOMPLETE'


def parse_report(stdout,code,run_id,expected_pid,duration_ms):
    require(code==0,'instrument_transport_not_successful')
    if isinstance(stdout,bytes):stdout=stdout.decode('utf-8','strict')
    text=stdout.replace('\r\n','\n')
    require(text.endswith('\n') and len([x for x in text.splitlines() if x.strip()])==2,'instrument_output_truncated_or_unexpected')
    results=list(re.finditer(r'(?m)^INSTRUMENTATION_RESULT: report_json=([^\n]*)$',text))
    codes=list(re.finditer(r'(?m)^INSTRUMENTATION_CODE: ([^\n]*)$',text))
    require(len(results)==len(codes)==1 and results[0].end()<codes[0].start(),'instrument_result_count_or_order')
    value=parse_progress(results[0].group(1),run_id,expected_pid)
    status=classify_observation(value,None,run_id,expected_pid,duration_ms)
    require(value['status'] in ('PASS','FAIL','INCOMPLETE') and status==value['status'] and
            codes[0].group(1)==('0' if status=='PASS' else '1'),'instrument_result_invalid')
    return value


def _host(command):
    p=subprocess.run([str(x) for x in command],capture_output=True,timeout=30,check=False)
    require(p.returncode==0,'local_apk_inspection_failed')
    return p.stdout.decode('utf-8','strict')


def verify_local(apk,aar,sdk_core,build,abi,tools):
    built=unique_json(build.read_text(encoding='utf-8-sig'))
    require(built.get('state')=='PASS_R8_BUILD_ONLY_NOT_DEVICE_EXECUTION' and built.get('device_executed') is False and
        built.get('stability_qualified') is False and built.get('performance_qualified') is False and built.get('abi')==abi and
        built.get('package_name')==PACKAGE and built.get('instrumentation')==INSTRUMENT and built.get('activity')==ACTIVITY and
        built.get('apk_sha256')==digest(apk) and built.get('apk_bytes')==apk.stat().st_size and
        built.get('inputs',{}).get('aar_sha256')==digest(aar),'build_receipt_scope_or_identity')
    for name in builder.SOURCES:
        path=workspace_file(ROOT/name); item=built.get('source_identities',{}).get(name,{})
        require(item.get('bytes')==path.stat().st_size and item.get('sha256')==digest(path),'build_source_drift')
    require(built.get('r8_evidence',{}).get('native_method_count')==37 and
            built['r8_evidence'].get('managed_native_signatures_retained') is True and
            built['r8_evidence'].get('shrink_witness_removed') is True,'r8_evidence_missing')
    with zipfile.ZipFile(apk) as app, zipfile.ZipFile(aar) as library:
        require(len(app.namelist())==len(set(app.namelist())) and len(library.namelist())==len(set(library.namelist())),'duplicate_package_member')
        libs=('libmgmem.so','libmgmem_pressure_jni.so')
        require(sorted(x for x in app.namelist() if x.endswith('.so'))==sorted('lib/'+abi+'/'+x for x in libs) and
                sorted(x for x in library.namelist() if x.endswith('.so'))==sorted('jni/'+abi+'/'+x for x in libs),'native_closure_mismatch')
        core,jni=(app.read('lib/'+abi+'/'+name) for name in libs)
        machine={'arm64-v8a':183,'x86_64':62}[abi]
        require(all(len(x)>=20 and x[:6]==b'\x7fELF\x02\x01' and int.from_bytes(x[18:20],'little')==machine for x in (core,jni)),'native_architecture_mismatch')
        require(core==library.read('jni/'+abi+'/'+libs[0])==sdk_core.read_bytes() and
                jni==library.read('jni/'+abi+'/'+libs[1]) and sha(core)==built['inputs']['shared_core_sha256'] and
                sha(jni)==built['inputs']['shared_jni_sha256'] and sha(library.read('classes.jar'))==built['inputs']['classes_jar_sha256'] and
                sha(app.read('classes.dex'))==built['r8_evidence']['dex_sha256'],'apk_member_identity_mismatch')
    certs=re.findall(r'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})',_host([tools['java'],'-jar',tools['apksigner'],'verify','--print-certs',apk]))
    require(len(certs)==1 and certs[0].lower()==built['certificate_sha256'],'local_apk_signer_mismatch')
    builder.verify_manifest(_host([tools['aapt2'],'dump','xmltree',apk,'--file','AndroidManifest.xml']),
                            _host([tools['aapt2'],'dump','badging',apk]),abi)
    operations=built.get('operations');require(isinstance(operations,list) and operations,'build_log_missing')
    logdir=build.parent/'logs';steps={}
    for row in operations:
        label=row.get('step');require(isinstance(label,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,160}',label) and label not in steps and row.get('state')=='EXITED' and row.get('exit_code')==0,'build_log_invalid')
        for stream in ('stdout','stderr'):
            path=workspace_file(logdir/(label+'.'+stream));require(digest(path)==row.get(stream+'_sha256'),'build_log_drift')
        steps[label]=logdir/(label+'.stdout')
    expected={base.JAVA_PACKAGE+klass:builder.native_declarations(steps['javap-'+klass].read_text(encoding='utf-8')) for klass in base.NATIVE_CLASSES}
    require(sum(map(len,expected.values()))==37,'jni_count_invalid')
    base.validate_dex(steps['dexdump'].read_text(encoding='utf-8'),expected)
    builder.verify_shrink_usage(workspace_file(build.parent/'r8-usage.txt').read_text(encoding='utf-8'))
    return built


def common_args(parser):
    for name in ('apk','aar','sdk-core','build-receipt','adb','aapt2','java','apksigner'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--serial',required=True)


def frozen_inputs(args):
    paths={k:workspace_file(getattr(args,k)) for k in ('apk','aar','sdk_core','build_receipt')}
    tools={k:Path(getattr(args,k)).resolve() for k in ('adb','aapt2','java','apksigner')}
    require(all(x.is_file() for x in tools.values()),'local_tool_missing')
    return paths,tools


def freeze(args):
    require(SERIAL.fullmatch(args.serial) and re.fullmatch(r'[0-9a-f]{16}',args.run_id),'invalid_target_identity')
    require(27<=args.api<=100 and args.abi in ('arm64-v8a','x86_64') and args.page_bytes in (4096,16384),'invalid_device_profile')
    require(900000<=args.duration_ms<=3600000 and 1<=args.max_rounds<=100000 and 1<=args.interval_ms<=60000 and
            1000<=args.phase_timeout_ms<=60000 and args.interval_ms<args.phase_timeout_ms and
            args.duration_ms//1000+30<=args.outer_seconds<=args.duration_ms//1000+600 and 1<=args.poll_seconds<=5,'invalid_stability_profile')
    paths,tools=frozen_inputs(args);built=verify_local(paths['apk'],paths['aar'],paths['sdk_core'],paths['build_receipt'],args.abi,tools)
    allfiles=[*paths.values(),*(ROOT/name for name in builder.SOURCES),Path(__file__),ROOT/'tools/test_android_managed_stability_round.py']
    entries=[common.identity(x) for x in allfiles]
    require(len({x['path'] for x in entries})==len(entries),'duplicate_frozen_input')
    value={'schema_version':1,'state':'FROZEN_BEFORE_DEVICE_EXECUTION','scope':'OWN_R8_FOREGROUND_SYNTHETIC_STABILITY_NO_BIL_NO_PERFORMANCE',
           'device_serial_sha256':sha(args.serial.encode()),'run_id':args.run_id,'device':{'api':args.api,'abi':args.abi,'page_bytes':args.page_bytes},
           'duration_ms':args.duration_ms,'max_rounds':args.max_rounds,'interval_ms':args.interval_ms,'phase_timeout_ms':args.phase_timeout_ms,
           'outer_seconds':args.outer_seconds,'poll_seconds':args.poll_seconds,'package':PACKAGE,'component':COMPONENT,'activity':ACTIVITY_COMPONENT,
           'certificate_sha256':built['certificate_sha256'],'apk_sha256':digest(paths['apk']),'aar_sha256':digest(paths['aar']),
           'core_sha256':built['inputs']['shared_core_sha256'],'jni_sha256':built['inputs']['shared_jni_sha256'],
           'build_receipt_sha256':digest(paths['build_receipt']),'tools':{k:digest(v) for k,v in tools.items()},'files':entries,
           'frozen_at_utc':utc(),'shared_device_lock':False}
    output=(ROOT/'build'/'managed-stability-plans'/secrets.token_hex(16)/'plan.json').resolve()
    require(not output.parent.exists(),'private_plan_directory_exists');output.parent.mkdir(parents=True,exist_ok=False)
    with output.open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();__import__('os').fsync(stream.fileno())
    print(json.dumps({'state':value['state'],'plan_sha256':digest(output),'plan_path':output.relative_to(ROOT).as_posix(),'plan_visibility':'PRIVATE_DO_NOT_PUBLISH'}))
    return output


def validate_plan(plan,args):
    require(plan.get('schema_version')==1 and plan.get('state')=='FROZEN_BEFORE_DEVICE_EXECUTION' and
            plan.get('package')==PACKAGE and plan.get('component')==COMPONENT and plan.get('activity')==ACTIVITY_COMPONENT and
            SHA.fullmatch(plan.get('device_serial_sha256','')) and sha(args.serial.encode())==plan['device_serial_sha256'] and
            re.fullmatch(r'[0-9a-f]{16}',plan.get('run_id','')),'plan_target_mismatch')
    paths,tools=frozen_inputs(args)
    required={x.resolve().relative_to(ROOT).as_posix() for x in [*paths.values(),*(ROOT/n for n in builder.SOURCES),Path(__file__),ROOT/'tools/test_android_managed_stability_round.py']}
    files=plan.get('files');require(isinstance(files,list) and len({x.get('path') for x in files})==len(files),'plan_files_invalid')
    locked={x['path']:x for x in files};require(required<=set(locked),'plan_missing_required_input')
    for entry in files:
        path=workspace_file(ROOT/entry['path']);require(path.stat().st_size==entry['bytes'] and digest(path)==entry['sha256'],'frozen_input_drift')
    require(set(plan.get('tools',{}))==set(tools) and all(digest(path)==plan['tools'][k] for k,path in tools.items()),'frozen_tool_drift')
    profile=plan.get('device',{});duration=plan.get('duration_ms',0)
    require(profile.get('abi') in ('arm64-v8a','x86_64') and 27<=profile.get('api',0)<=100 and profile.get('page_bytes') in (4096,16384) and
            900000<=duration<=3600000 and 1<=plan.get('max_rounds',0)<=100000 and 1<=plan.get('interval_ms',0)<plan.get('phase_timeout_ms',0)<=60000 and
            plan['phase_timeout_ms']>=1000 and duration//1000+30<=plan.get('outer_seconds',0)<=duration//1000+600 and 1<=plan.get('poll_seconds',0)<=5,'plan_profile_invalid')
    built=verify_local(paths['apk'],paths['aar'],paths['sdk_core'],paths['build_receipt'],profile['abi'],tools)
    require(digest(paths['apk'])==plan['apk_sha256'] and digest(paths['aar'])==plan['aar_sha256'] and
            digest(paths['sdk_core'])==plan['core_sha256'] and digest(paths['build_receipt'])==plan['build_receipt_sha256'] and
            built['certificate_sha256']==plan['certificate_sha256'] and built['inputs']['shared_jni_sha256']==plan['jni_sha256'],'plan_primary_identity_mismatch')
    return paths,tools


class Device(common.Device):
    def gate(self,label,own_pid=None,own_instrument=False):
        _,ps,_=self.command(label+'-processes',['shell','ps','-A','-o','PID,NAME,ARGS'])
        _,activity,_=self.command(label+'-activity',['shell','dumpsys','activity'])
        lines=[x.strip() for x in ps.splitlines() if x.strip()]
        require(lines and re.fullmatch(r'PID\s+NAME\s+ARGS',lines[0]),'process_inventory_header_missing')
        rows=[x.split(None,2) for x in lines[1:]]
        require(all(len(x)>=2 and x[0].isdigit() for x in rows) and len({x[0] for x in rows})==len(rows) and
                len(rows)>=5 and any(x[1]=='init' for x in rows) and any(x[1]=='system_server' for x in rows),'process_inventory_unparseable')
        require('ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)' in activity and
                'ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)' in activity and
                not re.search(r'(?i)permission\s+denial|permission\s+denied|can.t find service|error dumping service',activity),'activity_inventory_unparseable')
        busy=[x for x in rows if (BUSY.search(' '.join(x[1:])) or PACKAGE in ' '.join(x[1:])) and
              not (own_pid is not None and x[0]==str(own_pid) and x[1]==PACKAGE)]
        active=[]
        for line in activity.splitlines():
            if not re.search('activeinstrumentation',line,re.I):continue
            if re.search(r'\bmActiveInstrumentation\s*[:=]\s*(?:null|None|\[\]|\{\}|0)\s*$',line):continue
            active.append(line)
        bad=[line for line in active if not (own_instrument and PACKAGE in line and INSTRUMENT in line)]
        result={'name':label,'parsed_processes':len(rows),'matching_other_tests':len(busy),
                'other_or_unknown_instrumentation':len(bad),'result':'BLOCKED' if busy or bad else 'PASS_OBSERVATIONAL_ONLY'}
        self.receipt['gates'].append(result)
        require(result['result']=='PASS_OBSERVATIONAL_ONLY',label+'_device_busy_or_unknown')
    def pid(self):
        code,text,_=self.command('own-pid',['shell','pidof',PACKAGE],timeout=4,allow_nonzero=True)
        parts=text.split();require(code in (0,1) and all(x.isdigit() for x in parts) and len(parts)<=1,'own_pid_unparseable')
        return parts[0] if parts else None
    def process_identity(self,pid,uid):
        _,stat,_=self.command('own-process-stat',['shell','run-as',PACKAGE,'cat','/proc/'+pid+'/stat'],timeout=4)
        _,status,_=self.command('own-process-status',['shell','run-as',PACKAGE,'cat','/proc/'+pid+'/status'],timeout=4)
        end=stat.rfind(')');fields=stat[end+1:].split() if end>=0 else []
        match=re.search(r'(?m)^Uid:\s*(\d+)(?:\s|$)',status)
        require(stat.startswith(pid+' (') and len(fields)>=20 and fields[19].isdigit() and match and match.group(1)==uid,'own_process_identity_unavailable')
        return sha((pid+':'+uid+':'+fields[19]).encode())
    def freeze_sample(self,pid,uid):
        started = time.monotonic()
        expected='/uid_'+uid+'/pid_'+pid
        _,cgroup,_=self.command('own-cgroup',['shell','run-as',PACKAGE,'cat','/proc/'+pid+'/cgroup'],timeout=4)
        require('0::'+expected in cgroup.splitlines(),'own_cgroup_identity_unavailable')
        basepath='/sys/fs/cgroup'+expected
        _,mode,_=self.command('own-freeze',['shell','cat',basepath+'/cgroup.freeze'],timeout=4)
        _,events,_=self.command('own-events',['shell','cat',basepath+'/cgroup.events'],timeout=4)
        fields=dict(x.split(None,1) for x in events.splitlines() if len(x.split(None,1))==2)
        require(mode.strip() in ('0','1') and fields.get('frozen') in ('0','1'),'own_cgroup_state_unavailable')
        return {'mode':mode.strip(),'frozen':fields['frozen'],'observed_at_utc':utc(),
                'sample_started_monotonic': started, 'sample_finished_monotonic': time.monotonic()}
    def read_record(self,kind,run_id):
        require(kind in ('progress','stall') and re.fullmatch(r'[0-9a-f]{16}',run_id),'record_target_invalid')
        root='cache/managed-stability-'+run_id+'-'+kind+'.json'
        result=None
        for suffix in ('','.new','.bak'):
            code,out,err=self.command('own-'+kind+('-sidecar' if suffix else ''),['shell','run-as',PACKAGE,'cat',root+suffix],timeout=5,allow_nonzero=True)
            if code==0:
                require(not suffix,'record_atomic_write_in_progress');result=out
            else:
                require(code==1 and re.search(r'No such file or directory',err) and not out.strip(),'record_read_unknown')
        return result


def observe_once(device,run_id,expected_pid,uid,identity,duration_ms):
    device.gate('during-instrumentation', own_pid=expected_pid, own_instrument=True)
    pid=device.pid();require(pid is not None,'own_pid_missing')
    require(str(pid)==str(expected_pid),'own_pid_changed')
    require(device.process_identity(str(pid),uid)==identity,'own_process_identity_changed')
    _,activity,_=device.command('own-activity-resumed',['shell','dumpsys','activity','activities'],timeout=8)
    require(parse_resumed(activity),'own_activity_not_resumed')
    sample=device.freeze_sample(str(pid),uid)
    if sample['mode']=='1' or sample['frozen']=='1':
        device.receipt['freezer_observation']='FROZEN_OBSERVED'
        raise Blocked('own_cgroup_frozen')
    stall=device.read_record('stall',run_id)
    if stall is not None:
        parsed=parse_progress(stall,run_id,int(expected_pid))
        require(parsed['status']=='INCOMPLETE','stall_invalid');raise Blocked('own_stall_reported')
    progress=device.read_record('progress',run_id)
    if progress is None:return None,sample
    value=parse_progress(progress,run_id,int(expected_pid))
    require(value['foreground_confirmed'] is True,'in_process_foreground_not_confirmed')
    state=classify_observation(value,None,run_id,int(expected_pid),duration_ms)
    require(value['status']!='PASS' or state=='PASS','progress_pass_unqualified')
    require(value['status']!='INCOMPLETE','own_progress_incomplete')
    return value,sample


def admission_check(path,serial):
    path=workspace_file(path)
    require(path.is_relative_to((ROOT/'build').resolve()),'admission_must_be_private')
    data=unique_json(path.read_text(encoding='utf-8-sig'))
    require(set(data)=={'state','device_serial_sha256','observed_at_utc','visible_sessions_checked','shared_device_lock'} and
            data['state']=='CLEAR_OBSERVATIONAL_ONLY' and data['device_serial_sha256']==sha(serial.encode()) and
            data['visible_sessions_checked'] is True and data['shared_device_lock'] is False,'admission_invalid')
    try: stamp=datetime.fromisoformat(data['observed_at_utc'].replace('Z','+00:00'))
    except (ValueError,TypeError,AttributeError) as error:raise Blocked('admission_time_invalid') from error
    require(stamp.tzinfo is not None and -5<=(datetime.now(timezone.utc)-stamp).total_seconds()<=120,'admission_stale')
    return digest(path)


def package_inventory(device, private, plan, paths, tools, save):
    """Check all visible users and install only this package after a fresh gate."""
    _, boot, _ = device.command('boot-before', ['shell', 'cat', '/proc/sys/kernel/random/boot_id'])
    require(UUID.fullmatch(boot.strip()) is not None, 'boot_identity_unavailable')
    _, current, _ = device.command('current-user', ['shell', 'am', 'get-current-user'])
    user = current.strip()
    require(user.isdigit(), 'current_user_unavailable')
    _, text, _ = device.command('user-inventory', ['shell', 'pm', 'list', 'users'])
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    require(lines and lines[0] == 'Users:', 'user_inventory_unparseable')
    matches = [re.fullmatch(r'UserInfo\{(\d+):[^}]*\}(?:\s+.*)?', line) for line in lines[1:]]
    require(matches and all(matches) and len(matches) <= 32, 'user_inventory_unparseable')
    users = [m.group(1) for m in matches]
    require(user in users and len(users) == len(set(users)), 'user_inventory_ambiguous')
    presence = {}
    for candidate in users:
        _, listed, _ = device.command('package-inventory-user', ['shell', 'pm', 'list', 'packages', '--user', candidate, '-u'])
        rows = [line.strip() for line in listed.splitlines() if line.strip()]
        require(rows and all(re.fullmatch(r'package:[A-Za-z0-9_.]+', x) for x in rows), 'package_inventory_unparseable')
        presence[candidate] = ('package:' + PACKAGE) in rows
    code, before, stderr = device.command('package-path-before', ['shell', 'pm', 'path', PACKAGE], allow_nonzero=True)
    old_paths = common.parse_package_paths(before)
    require(code in (0, 1) and not stderr.strip() and bool(old_paths) == presence[user], 'existing_package_state_ambiguous')
    require(len(old_paths) <= 1, 'existing_package_path_ambiguous')
    old = private / 'installed-before.apk'
    if old_paths:
        device.command('pull-existing-own-apk', ['pull', old_paths[0], old], timeout=60)
        output = _host([tools['java'], '-jar', tools['apksigner'], 'verify', '--print-certs', old])
        certs = re.findall(r'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})', output)
        require(len(certs) == 1 and certs[0].lower() == plan['certificate_sha256'], 'existing_package_signer_mismatch')
    device.gate('before-install')
    if not old_paths or digest(old) != plan['apk_sha256']:
        require(not any(found for candidate, found in presence.items() if candidate != user), 'same_package_present_for_other_user')
        device.receipt['installation'] = 'REQUEST_IN_FLIGHT_OUTCOME_UNKNOWN'
        save()
        install = ['install'] + (['-r'] if old_paths else []) + ['--user', user, paths['apk']]
        _, result, _ = device.command('install-own-diagnostic', install, timeout=120)
        require(re.search(r'(?m)^Success\s*$', result) is not None, 'install_not_confirmed')
        device.receipt['installation'] = 'COMMAND_SUCCESS_INSTALLED_BYTES_NOT_YET_VERIFIED'
        save()
    else:
        device.receipt['installation'] = 'EXACT_APK_ALREADY_INSTALLED_NO_UPDATE'
    _, current_path, _ = device.command('package-path-after-install', ['shell', 'pm', 'path', PACKAGE])
    package_paths = common.parse_package_paths(current_path)
    require(len(package_paths) == 1, 'installed_package_path_unavailable')
    installed = private / 'installed-after.apk'
    device.command('pull-installed-own-apk', ['pull', package_paths[0], installed], timeout=60)
    require(digest(installed) == plan['apk_sha256'], 'installed_apk_bytes_mismatch')
    if device.receipt['installation'] != 'EXACT_APK_ALREADY_INSTALLED_NO_UPDATE':
        device.receipt['installation'] = 'OWN_PACKAGE_INSTALLED_AND_VERIFIED_NO_DATA_CLEAR'
    _, uid_text, _ = device.command('own-uid', ['shell', 'run-as', PACKAGE, 'id', '-u'])
    uid = uid_text.strip()
    require(uid.isdigit(), 'own_uid_unavailable')
    device.receipt.update(android_user_count=len(users), other_user_package_present=any(
        found for candidate, found in presence.items() if candidate != user), installed_apk_sha256=digest(installed))
    return user, uid, boot.strip(), package_paths


def verify_observations(samples, start, end, duration_ms):
    """Bound observed gaps; do not assert continuous foreground or freezer state."""
    require(len(samples) >= 2, 'insufficient_observations')
    intervals = [(s['begin'], s['end']) for s in samples]
    require(all(isinstance(t, (int, float)) and not isinstance(t, bool) for pair in intervals for t in pair) and
            all(start <= a <= b <= end for a, b in intervals) and
            all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:])), 'observation_time_invalid')
    # The two cgroup reads occur somewhere within each interval. Bound the
    # worst possible gap, rather than timestamping later progress reads.
    gaps = [b[1] - a[0] for a, b in zip(intervals, intervals[1:])]
    require(end - start >= duration_ms / 1000 and intervals[0][1] - start <= 30 and end - intervals[-1][0] <= 30 and
            all(gap <= 30 for gap in gaps), 'observation_coverage_insufficient')
    require(all(s['mode'] == '0' and s['frozen'] == '0' for s in samples), 'own_cgroup_frozen')
    return {'count': len(samples), 'max_gap_seconds': round(max(gaps), 3),
            'freezer_observation': 'DISCRETE_SAMPLES_UNFROZEN_NOT_CONTINUOUS'}


def monitor(child, device, plan, pid, uid, identity, started, monotonic=None, sleep=None):
    """A blocked observation is incomplete; never kill or clean a remote process."""
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    deadline = started + plan['outer_seconds']
    device.deadline = deadline
    samples = []
    last_sequence = -1
    while True:
        now = monotonic()
        require(now < deadline, 'instrument_outer_timeout')
        if child.poll() is not None:
            break
        try:
            progress, sample = observe_once(device, plan['run_id'], pid, uid, identity, plan['duration_ms'])
        except Blocked as error:
            if str(error) not in ('own_activity_not_resumed', 'own_pid_missing'):
                raise
            # The diagnostic itself finishes its Activity after the workload.
            # Only a same-run complete-phase record permits a short handoff to
            # the terminal report; identity, report and postflight still gate it.
            current = device.pid()
            require(samples and (current is None or (current == str(pid) and
                    device.process_identity(str(pid), uid) == identity)), 'finish_handoff_identity_changed')
            require(device.read_record('stall', plan['run_id']) is None, 'own_stall_reported')
            record = device.read_record('progress', plan['run_id'])
            require(record is not None, 'finish_handoff_progress_missing')
            progress = parse_progress(record, plan['run_id'], int(pid))
            require(progress['phase'] == 'complete' and progress['status'] in ('RUNNING', 'PASS') and
                    progress['foreground_confirmed'] is True and progress['sequence'] >= last_sequence,
                    'finish_handoff_not_confirmed')
            handoff_deadline = min(deadline, monotonic() + 8)
            while child.poll() is None and monotonic() < handoff_deadline:
                sleep(min(.2, max(0, handoff_deadline - monotonic())))
            require(child.poll() is not None and monotonic() < deadline, 'finish_handoff_timeout')
            break
        now = monotonic()
        require(now < deadline, 'observation_crossed_outer_timeout')
        if progress is not None:
            require(progress['sequence'] >= last_sequence, 'progress_sequence_regressed')
            last_sequence = progress['sequence']
        require(started <= sample['sample_started_monotonic'] <= sample['sample_finished_monotonic'] <= now,
                'sample_time_invalid')
        samples.append({'begin': sample['sample_started_monotonic'], 'end': sample['sample_finished_monotonic'],
                        'mode': sample['mode'], 'frozen': sample['frozen']})
        device.receipt['observation_sample_count'] = len(samples)
        if sample['mode'] == '1' or sample['frozen'] == '1':
            device.receipt['freezer_observation'] = 'FROZEN_OBSERVED'
            raise Blocked('own_cgroup_frozen')
        if child.poll() is None:
            sleep(min(plan['poll_seconds'], max(0, deadline - monotonic())))
    ended = monotonic()
    require(ended < deadline, 'instrument_outer_timeout')
    device.deadline = None
    return samples, ended


def run(args):
    require(SERIAL.fullmatch(args.serial) is not None, 'invalid_target_identity')
    plan_path = workspace_file(args.plan)
    require(plan_path.is_relative_to((ROOT / 'build').resolve()) and SHA.fullmatch(args.plan_sha256 or '') and
            digest(plan_path) == args.plan_sha256, 'frozen_plan_sha256_mismatch')
    plan = unique_json(plan_path.read_text(encoding='utf-8-sig'))
    paths, tools = validate_plan(plan, args)
    admission_sha = admission_check(args.admission, args.serial)
    out, private = Path(args.out_dir).resolve(), Path(args.private_dir).resolve()
    build = (ROOT / 'build').resolve()
    require(out.is_relative_to(build) and private.is_relative_to(build) and out != private and
            not out.is_relative_to(private) and not private.is_relative_to(out) and
            not out.exists() and not private.exists(), 'fresh_private_directories_required')
    out.mkdir(parents=True); private.mkdir(parents=True)
    receipt = {'schema_version': 1, 'state': 'PREPARING', 'scope': plan['scope'],
        'plan_sha256': args.plan_sha256, 'admission_sha256': admission_sha, 'apk_sha256': plan['apk_sha256'],
        'device': plan['device'], 'started_at_utc': utc(), 'gates': [], 'commands': [],
        'shared_device_lock': False, 'remote_process_state': 'UNKNOWN', 'performance': 'NOT_MEASURED',
        'report_validated': False, 'freezer_observation': 'NOT_OBSERVED',
        'foreground_window_state': 'NOT_STARTED'}
    save = lambda: atomic_receipt(out / 'receipt.json', receipt)
    save()
    device = Device(args, out, private, receipt)
    child = None; stdout_file = None; stderr_file = None
    try:
        device.gate('initial')
        _, api, _ = device.command('api', ['shell', 'getprop', 'ro.build.version.sdk'])
        _, abi, _ = device.command('abi', ['shell', 'getprop', 'ro.product.cpu.abi'])
        _, page, _ = device.command('page', ['shell', 'getconf', 'PAGESIZE'])
        require(api.strip().isdigit() and page.strip().isdigit() and
                {'api': int(api.strip()), 'abi': abi.strip(), 'page_bytes': int(page.strip())} == plan['device'], 'device_profile_mismatch')
        user, uid, boot, package_paths = package_inventory(device, private, plan, paths, tools, save)
        require(admission_check(args.admission, args.serial) == admission_sha, 'admission_drift')
        device.gate('before-activity')
        receipt['foreground_window_state'] = 'START_REQUEST_IN_FLIGHT_OUTCOME_UNKNOWN_KEEP_SCREEN_ON_POSSIBLE'
        save()
        _, activity_start, _ = device.command('start-own-activity', ['shell', 'am', 'start', '--user', user, '-W', '-n', ACTIVITY_COMPONENT], timeout=30)
        require(re.search(r'(?m)^Status:\s*ok\s*$', activity_start) and
                not re.search(r'(?im)^\s*(?:Error|Exception|Warning):', activity_start), 'activity_start_not_confirmed')
        pid = device.pid(); require(pid is not None, 'foreground_process_missing')
        identity = device.process_identity(pid, uid)
        _, resumed, _ = device.command('own-activity-resumed', ['shell', 'dumpsys', 'activity', 'activities'])
        require(parse_resumed(resumed), 'own_activity_not_resumed')
        require(device.pid() == pid and device.process_identity(pid, uid) == identity, 'foreground_process_changed')
        require(admission_check(args.admission, args.serial) == admission_sha, 'admission_drift')
        device.gate('before-instrumentation', own_pid=pid)
        # Detect an old run ID before instrumentation; never remove old app data.
        require(device.read_record('progress', plan['run_id']) is None and device.read_record('stall', plan['run_id']) is None,
                'run_id_already_present')
        stdout_file = (private / 'instrumentation.stdout').open('xb')
        stderr_file = (private / 'instrumentation.stderr').open('xb')
        command = [str(args.adb), '-s', args.serial, 'shell', 'am', 'instrument', '-w', '-r', '--no-restart',
                   '--user', user, '-e', 'run_id', plan['run_id'], '-e', 'expected_pid', pid]
        for key in ('duration_ms', 'max_rounds', 'interval_ms', 'phase_timeout_ms'):
            command.extend(['-e', key, str(plan[key])])
        command.append(COMPONENT)
        receipt['state'] = 'INSTRUMENTATION_IN_FLIGHT_OUTCOME_UNKNOWN'; save()
        started = time.monotonic()
        child = subprocess.Popen(command, stdout=stdout_file, stderr=stderr_file)
        samples, ended = monitor(child, device, plan, pid, uid, identity, started)
        stdout_file.close(); stderr_file.close(); stdout_file = stderr_file = None
        stdout = (private / 'instrumentation.stdout').read_bytes()
        stderr = (private / 'instrumentation.stderr').read_bytes()
        require(not stderr.strip(), 'instrument_stderr_nonempty')
        report = parse_report(stdout, child.returncode, plan['run_id'], int(pid), plan['duration_ms'])
        receipt['android_report'] = report
        receipt['report_validated'] = True
        receipt['transport'] = {'exit_code': child.returncode, 'stdout_sha256': sha(stdout), 'stderr_sha256': sha(stderr),
                                'elapsed_seconds': round(ended-started, 3)}
        require(device.read_record('stall', plan['run_id']) is None, 'own_stall_reported')
        progress = device.read_record('progress', plan['run_id'])
        require(progress is not None and parse_progress(progress, plan['run_id'], int(pid)) == report, 'terminal_progress_mismatch')
        if report['status'] == 'PASS':
            receipt['observation'] = verify_observations(samples, started, ended, plan['duration_ms'])
            receipt['freezer_observation'] = receipt['observation']['freezer_observation']
        device.gate('after', own_pid=pid)
        _, boot_after, _ = device.command('boot-after', ['shell', 'cat', '/proc/sys/kernel/random/boot_id'])
        _, current_after, _ = device.command('current-user-after', ['shell', 'am', 'get-current-user'])
        require(boot_after.strip() == boot and current_after.strip() == user, 'device_boot_or_user_changed')
        _, post_path, _ = device.command('package-path-after', ['shell', 'pm', 'path', PACKAGE])
        require(common.parse_package_paths(post_path) == package_paths, 'installed_package_changed')
        final = private / 'installed-postflight.apk'
        device.command('pull-postflight-own-apk', ['pull', package_paths[0], final], timeout=60)
        require(digest(final) == plan['apk_sha256'], 'postflight_apk_identity_mismatch')
        current_pid = device.pid()
        if current_pid is not None:
            require(current_pid == pid and device.process_identity(pid, uid) == identity, 'postflight_process_identity_changed')
            _, foreground, _ = device.command('postflight-resumed', ['shell', 'dumpsys', 'activity', 'activities'])
            receipt['foreground_window_state'] = ('OWN_ACTIVITY_RESUMED_KEEP_SCREEN_ON' if parse_resumed(foreground)
                                                  else 'OWN_PROCESS_PRESENT_ACTIVITY_NOT_RESUMED_MAY_REMAIN')
            require(receipt['foreground_window_state'] != 'OWN_ACTIVITY_RESUMED_KEEP_SCREEN_ON', 'own_activity_still_resumed')
        else:
            receipt['foreground_window_state'] = 'NO_OWN_PROCESS_OBSERVED'
        receipt['remote_process_state'] = 'OWN_PROCESS_MAY_REMAIN_NO_FORCE_STOP_NO_SHARED_LOCK'
        receipt['state'] = {'PASS': 'PASS_SCOPED_STABILITY_POSTFLIGHT', 'FAIL': 'FAIL_FUNCTIONAL_POSTFLIGHT',
                            'INCOMPLETE': 'INCOMPLETE_REPORTED_POSTFLIGHT'}[report['status']]
    except BaseException as error:
        receipt['state'] = 'INCOMPLETE_OR_BLOCKED'
        receipt['reason'] = common.safe_reason(error)
        if receipt.get('installation') in ('REQUEST_IN_FLIGHT_OUTCOME_UNKNOWN', 'COMMAND_SUCCESS_INSTALLED_BYTES_NOT_YET_VERIFIED'):
            receipt['installation_outcome'] = 'UNKNOWN_REQUIRES_READ_ONLY_RECHECK'
        if child is not None and child.poll() is None:
            child.terminate()  # Local ADB transport only, never a remote force-stop.
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: receipt['local_transport'] = 'TERMINATION_UNCONFIRMED'
            receipt['remote_process_state'] = 'UNKNOWN_LOCAL_TRANSPORT_ONLY_NO_FORCE_STOP'
    finally:
        if stdout_file and not stdout_file.closed: stdout_file.close()
        if stderr_file and not stderr_file.closed: stderr_file.close()
        receipt['finished_at_utc'] = utc()
        try: save()
        except Exception:
            print(json.dumps({'state': 'INCOMPLETE_RECEIPT_PUBLICATION'})); return 2
    print(json.dumps({'state': receipt['state'], 'reason': receipt.get('reason'),
                      'freezer_observation': receipt['freezer_observation'],
                      'foreground_window_state': receipt['foreground_window_state']}))
    return 0 if receipt['state'] == 'PASS_SCOPED_STABILITY_POSTFLIGHT' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    freezer = sub.add_parser('freeze', help='Freeze local inputs without ADB')
    common_args(freezer)
    for key in ('api', 'page-bytes'): freezer.add_argument('--' + key, type=int, required=True)
    freezer.add_argument('--abi', choices=('arm64-v8a', 'x86_64'), required=True)
    freezer.add_argument('--run-id', required=True)
    freezer.add_argument('--duration-ms', type=int, default=900000)
    freezer.add_argument('--max-rounds', type=int, default=10000)
    freezer.add_argument('--interval-ms', type=int, default=250)
    freezer.add_argument('--phase-timeout-ms', type=int, default=30000)
    freezer.add_argument('--outer-seconds', type=int, default=1020)
    freezer.add_argument('--poll-seconds', type=int, default=5)
    runner = sub.add_parser('run', help='Run the frozen own-package stability diagnostic')
    common_args(runner)
    for key in ('plan', 'admission', 'out-dir', 'private-dir'): runner.add_argument('--' + key, type=Path, required=True)
    runner.add_argument('--plan-sha256', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'freeze': freeze(args); return 0
        return run(args)
    except (Blocked, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        print(json.dumps({'state': 'INCOMPLETE_OR_BLOCKED', 'reason': common.safe_reason(error)})); return 2


if __name__ == '__main__':
    sys.exit(main())
