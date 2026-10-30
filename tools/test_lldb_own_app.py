#!/usr/bin/env python3
"""Offline regression for the own-app LLDB identity and detach safety gates.

The argv shape is reconstructed from an actually observed Android /proc
cmdline, with synthetic socket, cache tag and PID values. Raw device identifiers
and traces are deliberately not part of this test fixture. No adb is called.
"""
import copy
import unittest

from lldb_own_app import Blocked, all_threads_detached, complete_capture, exact_server_pids, parse_start_ticks, parse_status, same_process


TARGET = 12345
OWN_SERVER = 23456
OTHER_SERVER = 34567
SERVER = 'cache/sample-tag/lldb-server'
SOCKET = '/mgmem-lldb-0123456789abcdef'


def cmdline(socket=SOCKET, target=TARGET, executable='./' + SERVER, extra=()):
    return '\x00'.join([executable, 'gdbserver', 'unix-abstract://' + socket, '--attach', str(target), *extra]) + '\x00'


class Identity(unittest.TestCase):
    def test_exact_proc_argv_finds_tracer_even_when_ps_args_would_omit_executable(self):
        # Real Android ps ARGS omitted argv[0]. The new path never reads it;
        # pidof and/or the kernel tracer identify candidates, and /proc is exact.
        observed_proc = cmdline()
        calls = []
        def read_proc(pid):
            calls.append(pid)
            return observed_proc if pid == OWN_SERVER else ''
        self.assertEqual(exact_server_pids('', OWN_SERVER, TARGET, SERVER, SOCKET, read_proc), [OWN_SERVER])
        self.assertEqual(calls, [OWN_SERVER])

    def test_pidof_finds_own_server_before_target_has_a_tracer(self):
        read_proc = lambda pid: cmdline() if pid == OWN_SERVER else ''
        self.assertEqual(exact_server_pids(str(OWN_SERVER), 0, TARGET, SERVER, SOCKET, read_proc), [OWN_SERVER])

    def test_other_socket_and_target_and_argv_are_rejected(self):
        variants = [cmdline(socket='/different'), cmdline(target=TARGET+1),
                    cmdline(executable='./cache/other/lldb-server'), cmdline(extra=('--server',)),
                    'gdbserver\x00unix-abstract://' + SOCKET + '\x00--attach\x00' + str(TARGET)]
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(exact_server_pids(str(OTHER_SERVER), OTHER_SERVER, TARGET, SERVER, SOCKET, lambda _: value), [])

    def test_candidate_target_pid_is_never_considered_a_server(self):
        self.assertEqual(exact_server_pids(str(TARGET), TARGET, TARGET, SERVER, SOCKET, lambda _: cmdline()), [])

    def test_only_exact_server_wins_among_other_debuggers(self):
        def read_proc(pid):
            return cmdline() if pid == OWN_SERVER else cmdline(socket='/another-task')
        self.assertEqual(exact_server_pids(f'{OWN_SERVER} {OTHER_SERVER} {OWN_SERVER}', OTHER_SERVER,
                                           TARGET, SERVER, SOCKET, read_proc), [OWN_SERVER])


class Detach(unittest.TestCase):
    def setUp(self):
        self.before_uid = 10123
        self.snapshot = {'stable': True, 'threads': [
            {'tid': TARGET, 'uid': self.before_uid, 'tracer_pid': 0, 'state': 'S'},
            {'tid': TARGET+1, 'uid': self.before_uid, 'tracer_pid': 0, 'state': 'R'}]}
        self.receipt = {'state': 'CAPTURED', 'lldb': {'exit_code': 0, 'thread_lines': 2, 'frame_lines': 4},
                        'detach': 'REPORTED_BY_LLDB', 'detach_verified': True}

    def test_complete_stack_and_stable_untraced_threads_pass(self):
        self.assertTrue(all_threads_detached(self.before_uid, self.snapshot))
        self.assertTrue(complete_capture(self.receipt))

    def test_other_traced_or_stopped_thread_blocks_detach_even_if_main_is_clear(self):
        for key, value in [('tracer_pid', OWN_SERVER), ('state', 't'), ('state', 'T'), ('state', 'Z'), ('uid', self.before_uid+1)]:
            candidate = copy.deepcopy(self.snapshot); candidate['threads'][1][key] = value
            with self.subTest(key=key, value=value): self.assertFalse(all_threads_detached(self.before_uid, candidate))

    def test_missing_unstable_or_malformed_thread_inventory_never_passes(self):
        variants = [None, {}, {'stable': False, 'threads': self.snapshot['threads']},
                    {'stable': True, 'threads': []}, {'stable': True, 'threads': [{}]}]
        for value in variants:
            with self.subTest(value=value): self.assertFalse(all_threads_detached(self.before_uid, value))

    def test_missing_failed_or_unverified_detach_never_reports_success(self):
        self.assertFalse(complete_capture(None))
        self.assertFalse(complete_capture({'state': 'CAPTURED', 'lldb': None}))
        mutations = [('state', 'BLOCKED'), ('detach', 'NOT_CONFIRMED_BY_LLDB'), ('detach_verified', False)]
        for key, value in mutations:
            candidate = copy.deepcopy(self.receipt); candidate[key] = value
            with self.subTest(key=key): self.assertFalse(complete_capture(candidate))
        for key, value in [('exit_code', None), ('exit_code', 1), ('thread_lines', 0), ('frame_lines', 0)]:
            candidate = copy.deepcopy(self.receipt); candidate['lldb'][key] = value
            with self.subTest(key=key, value=value): self.assertFalse(complete_capture(candidate))

    def test_status_parser_fails_closed_when_required_fields_missing(self):
        value = 'Name:\texample\nState:\tS (sleeping)\nTracerPid:\t0\nUid:\t10123\t10123\t10123\t10123\n'
        self.assertEqual(parse_status(value), {'uid': 10123, 'tracer_pid': 0, 'state': 'S'})
        with self.assertRaises(Blocked): parse_status('State:\tS\nUid:\t10123\n')

    def test_pid_reuse_and_stat_truncation_never_pass(self):
        # After the parenthesized comm, fields start at #3; #22 is index 19.
        fields = ['S'] + ['0'] * 18 + ['86420'] + ['0'] * 4
        self.assertEqual(parse_start_ticks(str(TARGET) + ' (a tricky) name) ' + ' '.join(fields), TARGET), 86420)
        with self.assertRaises(Blocked): parse_start_ticks(str(TARGET+1) + ' (a) ' + ' '.join(fields), TARGET)
        with self.assertRaises(Blocked): parse_start_ticks(str(TARGET) + ' (a) S 1 2', TARGET)
        self.assertTrue(same_process({'uid': 10123, 'start_ticks': 86420}, {'uid': 10123, 'start_ticks': 86420}))
        self.assertFalse(same_process({'uid': 10123, 'start_ticks': 86420}, {'uid': 10123, 'start_ticks': 86421}))
        self.assertFalse(same_process({'uid': 10123}, {'uid': 10123}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
