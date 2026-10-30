"""Freeze one functional Android diagnostic profile before any device run."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,hashlib,json,zipfile
ROOT=Path(__file__).resolve().parents[1]
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def identity(p):return {'path':p.resolve().relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':digest(p)}
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build-receipt',type=Path,required=True);p.add_argument('--native-source',type=Path,required=True)
    p.add_argument('--native-build-receipt',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--minimum-ms',type=int,required=True)
    p.add_argument('--wall-ms',type=int,required=True);p.add_argument('--outer-seconds',type=int,required=True)
    p.add_argument('--iterations',type=int,default=1000);p.add_argument('--poll-seconds',type=float,default=2)
    p.add_argument('--stall-seconds',type=int,default=30);p.add_argument('--purpose',choices=['same_apk_background_foreground','retryfix_foreground_smoke','retryfix_foreground_long'],required=True)
    p.add_argument('--foreground-prestart',action='store_true',help='Foreground variant starts protected own Activity then instruments without process restart')
    p.add_argument('--require-reason-checks',action='store_true',help='Require real Java logical-unavailability reason checks')
    a=p.parse_args();out=a.output.resolve()
    if not out.is_relative_to(ROOT/'artifacts/android-close-investigation-20261030') or out.exists():p.error('fresh scoped plan required')
    if not 0<=a.minimum_ms<a.wall_ms<=1800000 or not 30<=a.outer_seconds<=2100 or not 1<=a.iterations<=100000 or not 1<=a.poll_seconds<=5 or not 10<=a.stall_seconds<=120:p.error('invalid bounds')
    build=json.loads(a.build_receipt.read_text(encoding='utf-8'))
    if build.get('status')!='PASS' or build.get('abi')!='arm64-v8a':raise RuntimeError('real arm64 build required')
    files={Path(__file__).resolve(),ROOT/'tools/run_android_close_round.py',ROOT/'tools/run_continuation_device.py',
           ROOT/'tools/run_arm64_validation.py',ROOT/'tools/verify_dex_import.py',a.build_receipt.resolve(),a.native_source.resolve(),a.native_build_receipt.resolve()}
    for entry in build['sources']+build['artifacts']+[build['input_aar'],build['base_aar']]:
        path=(ROOT/entry['path']).resolve()
        if not path.is_relative_to(ROOT) or digest(path)!=entry['sha256']:raise RuntimeError('build identity drift:'+entry['path'])
        files.add(path)
    native=next(i for i in build['aar_members'] if i['member'].endswith('/libmgmem_pressure_jni.so'))
    native_build=json.loads(a.native_build_receipt.read_text(encoding='utf-8'))
    if native_build.get('status')!='PASS':raise RuntimeError('native build receipt not PASS')
    original=next(i for i in native_build['sources'] if i['path']=='adapters/android/native/pressure_jni.cpp')
    native_artifact=next(i for i in native_build['artifacts'] if i['path'].endswith('/libmgmem_pressure_jni.so'))
    if original['sha256']!=digest(a.native_source) or native_artifact['sha256']!=native['sha256']:
        raise RuntimeError('native source or DSO does not match recorded AAR build')
    native_file=(ROOT/native_artifact['path']).resolve()
    if not native_file.is_relative_to(ROOT) or digest(native_file)!=native_artifact['sha256']:raise RuntimeError('native DSO drift')
    files.add(native_file)
    apk=next(ROOT/i['path'] for i in build['artifacts'] if i['path'].endswith('.apk'))
    with zipfile.ZipFile(apk) as archive:
        if hashlib.sha256(archive.read('lib/arm64-v8a/libmgmem_pressure_jni.so')).hexdigest()!=native['sha256']:raise RuntimeError('JNI_in_APK_differs_from_AAR')
    value={'state':'FROZEN_BEFORE_DEVICE_EXECUTION','scope':'FUNCTIONAL_DIAGNOSTIC_NOT_PERFORMANCE','purpose':a.purpose,
      'frozen_at_utc':datetime.now(timezone.utc).isoformat(),'build_receipt_sha256':digest(a.build_receipt),
      'minimum_ms':a.minimum_ms,'wall_ms':a.wall_ms,'outer_seconds':a.outer_seconds,'iterations':a.iterations,
      'poll_seconds':a.poll_seconds,'stall_seconds':a.stall_seconds,'variants':['background','foreground'] if a.purpose=='same_apk_background_foreground' else ['foreground'],
      'foreground_prestart':a.foreground_prestart,
      'reason_checks_required':a.require_reason_checks,
      'APK_sha256':digest(apk),'native_source_sha256':digest(a.native_source),'native_library_sha256':native['sha256'],
      'cgroup_rule':'Only own PID; observed freeze 1 or frozen 1, or start/inter-sample/end gap over 10 seconds, aborts qualification. Sampling is observational, not continuous proof.',
      'foreground_rule':'Opt-in own Activity; must resume and finish, no global freezer/power policy change. Bootstrap and process-restart behavior are recorded explicitly.',
      'ownership_rule':'Preflight and postflight process/instrumentation observation, no shared lock guarantee.',
      'resource_rule':'Do not combine domain logical capacity with RSS/PSS or claim performance/net benefit.',
      'files':[identity(path) for path in sorted(files)]}
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'state':value['state'],'plan_sha256':digest(out),'files':len(files),'APK_sha256':value['APK_sha256']}))
if __name__=='__main__':main()
