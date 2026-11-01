#!/usr/bin/env python3
"""Offline checks for JVM version detection and bounded stderr classification."""
import unittest
import contextlib
import io
import json
import sys
from types import SimpleNamespace
from unittest import mock

import run_host_probe as subject


LOAD = (b'WARNING: A restricted method in java.lang.System has been called\n'
        b'WARNING: java.lang.System::loadLibrary has been called by example.Probe in an unnamed module (file:/example/)\n'
        b'WARNING: Use --enable-native-access=ALL-UNNAMED to avoid a warning for callers in this module\n'
        b'WARNING: Restricted methods will be blocked in a future release unless native access is enabled\n')
BIND = (b'WARNING: A native method in example.Probe has been bound\n'
        b'WARNING: example.Probe::nativeMethod in an unnamed module has been called by example.Caller in an unnamed module (file:/example/)\n'
        b'WARNING: Use --enable-native-access=ALL-UNNAMED to avoid a warning for native methods declared in example.Probe\n'
        b'WARNING: Native methods will be blocked in a future release unless native access is enabled\n')


class HostLauncherChecks(unittest.TestCase):
    def test_java_major(self):
        for version, expected in [('17', 17), ('24', 24), ('25', 25), ('1.8', None)]:
            text=('    java.specification.version = '+version+'\n').encode()
            with self.subTest(version=version):
                if expected is None:
                    with self.assertRaises(RuntimeError):subject.parse_java_specification(b'', text)
                else:
                    self.assertEqual(subject.parse_java_specification(b'', text), expected)
        for raw in (b'', b'java.specification.version = 25\njava.specification.version = 17\n',
                    b'java.specification.version = future\n'):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                subject.parse_java_specification(b'', raw)

    def test_exact_known_warning_blocks(self):
        self.assertEqual(subject.classify_native_access_warning(b''), 'EMPTY')
        for raw in (LOAD, BIND, LOAD+BIND, LOAD.replace(b'\n', b'\r\n')):
            with self.subTest(raw=raw):
                self.assertEqual(subject.classify_native_access_warning(raw), 'JDK_NATIVE_ACCESS_WARNING_ONLY')

    def test_unknown_stderr_not_accepted(self):
        for raw in (LOAD+b'Exception: failed\n', LOAD+b'WARNING: unexpected\n', LOAD[:-1].split(b'\n', 1)[0],
                    b'Picked up JAVA_TOOL_OPTIONS: value\n', b'\xff'):
            with self.subTest(raw=raw):
                self.assertEqual(subject.classify_native_access_warning(raw), 'OTHER_STDERR')

    def test_version_probe_requires_success(self):
        result=mock.Mock(returncode=1, stdout=b'', stderr=b'java.specification.version = 25\n')
        with mock.patch.object(subject.subprocess, 'run', return_value=result), self.assertRaises(RuntimeError):
            subject.observe_java_version('java')

    def test_jdk25_control_and_explicit_native_access(self):
        report={'status':'PASS','suite':'android-native-borrow-v2','checks':62,
                'normal_domains_final_backing_bytes':0,'quarantined_failure_backing_bytes':32,
                'quarantined_failure_owner_pins':2,'quarantined_failure_reclaimed':False}
        control=SimpleNamespace(returncode=0,stdout=b'control output',stderr=LOAD)
        formal=SimpleNamespace(returncode=0,stdout=b'formal output',stderr=b'')
        out=io.StringIO()
        argv=['run_host_probe.py','--java','java','--classes','classes','--libraries','libs']
        with mock.patch.object(sys,'argv',argv), mock.patch.object(subject,'observe_java_version',return_value=25), \
             mock.patch.object(subject.subprocess,'run',side_effect=[control,formal]) as run, \
             mock.patch.object(subject,'parse_report',return_value=report), contextlib.redirect_stdout(out):
            subject.main()
        self.assertEqual(run.call_count,2)
        self.assertNotIn('--enable-native-access=ALL-UNNAMED',run.call_args_list[0].args[0])
        self.assertIn('--enable-native-access=ALL-UNNAMED',run.call_args_list[1].args[0])
        lines=[json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(lines[0]['stderr_class'],'JDK_NATIVE_ACCESS_WARNING_ONLY')
        self.assertEqual(lines[1]['state'],'PASS_HOST_REAL_JVM_JNI_ANDROID_STUBS_NOT_ART')
        self.assertNotIn('file:/example/',out.getvalue())

    def test_jdk17_does_not_receive_new_option(self):
        report={'status':'PASS','suite':'android-native-borrow-v2','checks':62,
                'normal_domains_final_backing_bytes':0,'quarantined_failure_backing_bytes':32,
                'quarantined_failure_owner_pins':2,'quarantined_failure_reclaimed':False}
        argv=['run_host_probe.py','--java','java','--classes','classes','--libraries','libs']
        with mock.patch.object(sys,'argv',argv), mock.patch.object(subject,'observe_java_version',return_value=17), \
             mock.patch.object(subject.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'ok',stderr=b'')) as run, \
             mock.patch.object(subject,'parse_report',return_value=report), contextlib.redirect_stdout(io.StringIO()):
            subject.main()
        self.assertEqual(run.call_count,1)
        self.assertNotIn('--enable-native-access=ALL-UNNAMED',run.call_args.args[0])

    def test_unknown_control_stderr_blocks_formal_run(self):
        argv=['run_host_probe.py','--java','java','--classes','classes','--libraries','libs']
        output=io.StringIO()
        with mock.patch.object(sys,'argv',argv), mock.patch.object(subject,'observe_java_version',return_value=25), \
             mock.patch.object(subject.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'ok',stderr=b'Exception in thread: UnsatisfiedLinkError /home/private\n')) as run, \
             contextlib.redirect_stdout(output), self.assertRaises(RuntimeError):
            subject.main()
        self.assertEqual(run.call_count,1)
        value=json.loads(output.getvalue())
        self.assertEqual(value['stderr_class'],'OTHER_STDERR')
        self.assertTrue(value['stderr_signals']['unsatisfied_link'])
        self.assertTrue(value['stderr_signals']['exception'])
        self.assertNotIn('/home/private',output.getvalue())

    def test_formal_stderr_is_always_rejected_with_safe_metadata(self):
        report={'status':'PASS','suite':'android-native-borrow-v2','checks':62,
                'normal_domains_final_backing_bytes':0,'quarantined_failure_backing_bytes':32,
                'quarantined_failure_owner_pins':2,'quarantined_failure_reclaimed':False}
        argv=['run_host_probe.py','--java','java','--classes','classes','--libraries','libs']
        control=SimpleNamespace(returncode=0,stdout=b'control',stderr=LOAD)
        formal=SimpleNamespace(returncode=0,stdout=b'formal',stderr=b'Picked up JAVA_TOOL_OPTIONS: /home/private\n')
        output=io.StringIO()
        with mock.patch.object(sys,'argv',argv), mock.patch.object(subject,'observe_java_version',return_value=25), \
             mock.patch.object(subject.subprocess,'run',side_effect=[control,formal]), \
             mock.patch.object(subject,'parse_report',return_value=report), \
             contextlib.redirect_stdout(output), self.assertRaises(RuntimeError):
            subject.main()
        records=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[-1]['stage'],'native_access_formal_failure')
        self.assertTrue(records[-1]['stderr_signals']['java_options_env'])
        self.assertNotIn('/home/private',output.getvalue())


if __name__ == '__main__':
    unittest.main()
