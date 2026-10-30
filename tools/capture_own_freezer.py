"""Read only the diagnostic app's own cgroup freezer and task wait channels."""
import argparse
from datetime import datetime,timezone
import hashlib,json,re,subprocess,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PACKAGE='io.github.jichuo1.meguviz.memory.pressureprobe.r8'
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--adb',type=Path,required=True);p.add_argument('--serial',required=True)
    p.add_argument('--pid-file',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--observations',type=int,default=2);p.add_argument('--interval-seconds',type=int,default=2)
    args=p.parse_args();pid=args.pid_file.read_text().strip()
    if not pid.isdigit() or not 1<=args.observations<=5 or not 1<=args.interval_seconds<=10:raise ValueError('bad bounded target')
    out=args.output.resolve()
    if not out.is_relative_to(ROOT/'artifacts/android-close-investigation-20261030') or out.exists():raise ValueError('new scoped evidence file required')
    def adb(*cmd):
        done=subprocess.run([str(args.adb),'-s',args.serial,'shell',*cmd],capture_output=True,timeout=10)
        if done.returncode:raise RuntimeError('read_only_adb_failed')
        return done.stdout.decode('utf-8','replace').strip()
    def own(*cmd):return adb('run-as',PACKAGE,*cmd)
    cmd=own('cat',f'/proc/{pid}/cmdline').split('\x00')[0]
    if cmd!=PACKAGE:raise RuntimeError('own_process_identity_changed')
    status=own('cat',f'/proc/{pid}/status');uid=re.search(r'(?m)^Uid:\s*(\d+)',status)
    if not uid:raise RuntimeError('target_uid_unavailable')
    expected=f'/uid_{uid.group(1)}/pid_{pid}'
    cgroups=own('cat',f'/proc/{pid}/cgroup').splitlines()
    if '0::'+expected not in cgroups:raise RuntimeError('expected_own_pid_cgroup_unavailable')
    cgroup='/sys/fs/cgroup'+expected;observations=[]
    for index in range(args.observations):
        names=own('ls',f'/proc/{pid}/task').split();waits=[]
        for tid in names:
            if not tid.isdigit():continue
            try:waits.append(own('cat',f'/proc/{pid}/task/{tid}/wchan'))
            except RuntimeError:pass
        event=adb('cat',cgroup+'/cgroup.events');freeze=adb('cat',cgroup+'/cgroup.freeze')
        observations.append({'at_utc':datetime.now(timezone.utc).isoformat(),'cgroup_freeze':freeze,
          'cgroup_events':event.splitlines(),'observed_thread_count':len(waits),
          'freezer_wait_thread_count':sum(w=='do_freezer_trap' for w in waits),
          'wait_channels':{w:waits.count(w) for w in sorted(set(waits))}})
        if index+1<args.observations:time.sleep(args.interval_seconds)
    value={'state':'OBSERVED','scope':'OWN_PID_CGROUP_READ_ONLY_NOT_DEVICE_WIDE',
      'device_serial_sha256':hashlib.sha256(args.serial.encode()).hexdigest(),
      'target_pid_sha256':hashlib.sha256(pid.encode()).hexdigest(),
      'cgroup_path_sha256':hashlib.sha256(expected.encode()).hexdigest(),
      'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
      'observations':observations,'freeze_controller_modified':False,'system_policy_modified':False,
      'qualifications':'KERNEL_FROZEN_STATE_OBSERVED_NOT_IDENTITY_OF_ACTOR_OR_ALL_PRIOR_HANGS'}
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'state':value['state'],'observations':observations}))
if __name__=='__main__':main()
