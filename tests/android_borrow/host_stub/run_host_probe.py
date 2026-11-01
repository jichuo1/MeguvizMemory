#!/usr/bin/env python3
"""Run the real fixture/JNI on a host JVM with Android APIs stubbed, not ART."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools'))
from android_borrow_round import parse_report  # noqa: E402


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java',type=Path,required=True)
    parser.add_argument('--classes',type=Path,required=True)
    parser.add_argument('--libraries',type=Path,required=True)
    args=parser.parse_args()
    run=subprocess.run([str(args.java),'-Djava.library.path='+str(args.libraries),
                        '-cp',str(args.classes),'HostProbe'],capture_output=True,timeout=25,check=False)
    if run.stderr:
        raise RuntimeError('host_fixture_stderr_nonempty')
    report=parse_report(run.stdout,run.returncode)
    if report['status']!='PASS':
        raise RuntimeError('host_fixture_report_failed')
    print(json.dumps({'state':'PASS_HOST_REAL_JVM_JNI_ANDROID_STUBS_NOT_ART','suite':report['suite'],
        'checks':report['checks'],'normal_backing_bytes':report['normal_domains_final_backing_bytes'],
        'quarantined_backing_bytes':report['quarantined_failure_backing_bytes'],
        'quarantined_owner_pins':report['quarantined_failure_owner_pins'],
        'quarantine_reclaimed':report['quarantined_failure_reclaimed']}))


if __name__=='__main__':
    main()
