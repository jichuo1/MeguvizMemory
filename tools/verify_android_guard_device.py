"""Exercise the diagnostic main-thread timeout guard on an idle owned device.

An expected own-process fail-stop proves the guard, never normal domain cleanup.
No foreign app, global setting, data clearing or uninstall is performed.
"""
import argparse
import json
from pathlib import Path
import time
from run_continuation_device import Continuation, ROOT, COMPONENT, PACKAGE, require, digest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('adb','admission','out-dir','sdk','java-home','r8-receipt'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--serial',required=True);args=parser.parse_args()
    args.out_dir=args.out_dir.resolve();require(args.out_dir.is_relative_to(ROOT/'artifacts/continuation-20261030'),'guard_output_scope')
    args.abi='x86_64';args.android_only=True;args.mode='guard';args.plan=None
    runner=Continuation(args);runner.receipt['source_sha256']=digest(Path(__file__))
    try:
        runner.stage();started=time.monotonic()
        command=runner.command('expected-own-process-isolation',['shell','am','instrument','-w','-r','-e','iterations','3','-e','observe_ams','false','-e','inject_main_running_timeout','true','-e','max_duration_ms','15000',COMPONENT],timeout=25)
        require(not command['timed_out'],'guard_transport_timeout')
        result=runner.checked('read-own-guard-result',['shell','run-as',PACKAGE,'cat','cache/mgmem-r8-result.json'])
        observed=json.loads(result['stdout'])
        require(observed['diagnostic_revision']==3 and observed['status']=='UNKNOWN' and observed['unsafe_cleanup'] is True,'guard_not_UNKNOWN')
        require(observed['reason']=='MAIN_TASK_RUNNING_TIMEOUT' and observed['phase']=='main.injected-running-latch','guard_wrong_boundary')
        require(observed['native_resource_state']=='UNKNOWN_NOT_FORCED_FREE' and observed['exit_scope']=='OWN_DIAGNOSTIC_PROCESS_FAIL_STOP_NO_CONCURRENT_FREE','guard_false_cleanup_claim')
        require(time.monotonic()-started<15,'guard_not_bounded')
        runner.occupancy('after',{})
        runner.receipt.update(state='PASS_EXPECTED_DIAGNOSTIC_FAIL_STOP',all_passed=True,guard_result=observed,normal_domain_close_qualified=False,actual_elapsed_seconds=time.monotonic()-started)
    except Exception as error:runner.receipt.update(state='FAIL_OR_INCOMPLETE',reason=str(error))
    runner.save();print(json.dumps({'state':runner.receipt['state'],'normal_domain_close_qualified':False}))
    raise SystemExit(0 if runner.receipt['all_passed'] else 2)

if __name__=='__main__':main()
