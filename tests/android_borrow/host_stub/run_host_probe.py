#!/usr/bin/env python3
"""Run the real fixture/JNI on a host JVM with Android APIs stubbed, not ART."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools'))
from android_borrow_round import parse_report  # noqa: E402


def parse_java_specification(stdout, stderr):
    text = (stdout + b'\n' + stderr).decode('utf-8', errors='replace')
    versions = re.findall(r'(?m)^\s*java\.specification\.version\s*=\s*(\d+(?:\.\d+)?)\s*$', text)
    if len(versions) != 1:
        raise RuntimeError('java_specification_version_unavailable')
    value = versions[0]
    major = int(value.split('.', 1)[1] if value.startswith('1.') else value.split('.', 1)[0])
    if major < 9:
        raise RuntimeError('java_version_unsupported')
    return major


def classify_native_access_warning(stderr):
    """Classify only complete, known JDK warning blocks; never expose text/paths."""
    if not stderr:
        return 'EMPTY'
    text = stderr.decode('utf-8', errors='replace').replace('\r\n', '\n')
    lines = [line for line in text.split('\n') if line]
    if len(lines) == 0 or len(lines) % 4:
        return 'OTHER_STDERR'
    for index in range(0, len(lines), 4):
        head, caller, hint, tail = lines[index:index + 4]
        load = (head == 'WARNING: A restricted method in java.lang.System has been called' and
                re.fullmatch(r'WARNING: java\.lang\.System::load(?:Library)? has been called by .+ in an unnamed module \(.+\)', caller) and
                hint == 'WARNING: Use --enable-native-access=ALL-UNNAMED to avoid a warning for callers in this module' and
                tail == 'WARNING: Restricted methods will be blocked in a future release unless native access is enabled')
        binding = (re.fullmatch(r'WARNING: A native method in [A-Za-z0-9_.$]+ has been bound', head) and
                   re.fullmatch(r'WARNING: .+ has been called by .+ in an unnamed module \(.+\)', caller) and
                   re.fullmatch(r'WARNING: Use --enable-native-access=ALL-UNNAMED to avoid a warning for native methods declared in .+', hint) and
                   tail == 'WARNING: Native methods will be blocked in a future release unless native access is enabled')
        if not (load or binding):
            return 'OTHER_STDERR'
    return 'JDK_NATIVE_ACCESS_WARNING_ONLY'


def stderr_metadata(stderr):
    """Fixed signal flags only; never include captured text or environment paths."""
    lower = stderr.lower()
    return {'stderr_bytes':len(stderr),'stderr_sha256':hashlib.sha256(stderr).hexdigest(),
            'stderr_class':classify_native_access_warning(stderr),
            'stderr_signals':{
                'native_access': b'--enable-native-access' in lower or b'restricted method' in lower or b'illegal native access' in lower,
                'unsatisfied_link': b'unsatisfiedlinkerror' in lower,
                'missing_class': b'classnotfoundexception' in lower or b'noclassdeffounderror' in lower,
                'fatal_vm': b'fatal error' in lower or (b'java runtime environment' in lower and b'failed' in lower),
                'java_options_env': any(x in lower for x in (b'java_tool_options',b'_java_options',b'jdk_java_options')),
                'exception': b'exception' in lower,
            }}


def observe_java_version(java):
    version = subprocess.run([str(java), '-XshowSettings:properties', '-version'],
                             capture_output=True, timeout=10, check=False)
    if version.returncode != 0:
        raise RuntimeError('java_version_probe_failed')
    return parse_java_specification(version.stdout, version.stderr)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java',type=Path,required=True)
    parser.add_argument('--classes',type=Path,required=True)
    parser.add_argument('--libraries',type=Path,required=True)
    args=parser.parse_args()
    major=observe_java_version(args.java)
    base=['-Djava.library.path='+str(args.libraries),'-cp',str(args.classes),'HostProbe']
    if major >= 24:
        # Independent diagnostic control: same host fixture without the option.
        # Never print captured JVM text, which can contain environment paths.
        control=subprocess.run([str(args.java),*base],capture_output=True,timeout=25,check=False)
        diagnostic=stderr_metadata(control.stderr)
        classification=diagnostic['stderr_class']
        print(json.dumps({'stage':'native_access_control','java_specification':major,
            'exit_code':control.returncode,'stdout_sha256':hashlib.sha256(control.stdout).hexdigest(),
            **diagnostic}),flush=True)
        if control.returncode != 0 or classification == 'OTHER_STDERR':
            raise RuntimeError('host_fixture_control_failed_or_unclassified_stderr')
        try:
            observed=parse_report(control.stdout,control.returncode)
        except (ValueError, RuntimeError, UnicodeDecodeError) as error:
            raise RuntimeError('host_fixture_control_report_invalid') from error
        if observed['status'] != 'PASS':
            raise RuntimeError('host_fixture_control_report_failed')
    options=['--enable-native-access=ALL-UNNAMED'] if major >= 24 else []
    run=subprocess.run([str(args.java),*options,*base],capture_output=True,timeout=25,check=False)
    if run.stderr or run.returncode != 0:
        print(json.dumps({'stage':'native_access_formal_failure','java_specification':major,
            'exit_code':run.returncode,'stdout_sha256':hashlib.sha256(run.stdout).hexdigest(),
            **stderr_metadata(run.stderr)}),flush=True)
    if run.stderr:
        raise RuntimeError('host_fixture_stderr_nonempty')
    report=parse_report(run.stdout,run.returncode)
    if report['status']!='PASS':
        raise RuntimeError('host_fixture_report_failed')
    print(json.dumps({'state':'PASS_HOST_REAL_JVM_JNI_ANDROID_STUBS_NOT_ART','suite':report['suite'],
        'java_specification':major,'native_access_explicit':major >= 24,
        'checks':report['checks'],'normal_backing_bytes':report['normal_domains_final_backing_bytes'],
        'quarantined_backing_bytes':report['quarantined_failure_backing_bytes'],
        'quarantined_owner_pins':report['quarantined_failure_owner_pins'],
        'quarantine_reclaimed':report['quarantined_failure_reclaimed']}))


if __name__=='__main__':
    main()
