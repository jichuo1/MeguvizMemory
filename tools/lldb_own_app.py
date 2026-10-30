#!/usr/bin/env python3
"""Bounded own-app LLDB capture. Raw outputs stay in a new local build directory.

Prerequisites: the caller has authorized the device, independently confirmed
that the debuggable target app/PID belongs to this task, and already installed a
known executable lldb-server in that app's cache. No device-wide settings or
other app/process/service is changed. This script may pause the selected PID.

The attached process can remain in an unknown/stopped state after transport or
ptrace failures. Detach is explicitly attempted; only the exact unique server
and verified forward created by this script are eligible for cleanup. Never
kill or force-stop the target to manufacture a successful detach.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'build'
PACKAGE = re.compile(r'io\.github\.jichuo1\.meguviz\.memory\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*\Z')
SERIAL = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
SERVER = re.compile(r'cache/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')


class Blocked(Exception):
    pass


def exact_server_pids(pidof_text, tracer_pid, target_pid, server_path, socket, read_cmdline):
    """Verify NUL-separated /proc argv, never Android ps's truncated ARGS."""
    candidates = {int(value) for value in pidof_text.split() if value.isdigit()}
    if tracer_pid > 0:
        candidates.add(tracer_pid)
    expected = ['./' + server_path, 'gdbserver', 'unix-abstract://' + socket, '--attach', str(target_pid)]
    result = []
    for pid in sorted(candidates):
        if pid == target_pid:
            continue
        argv = [value for value in read_cmdline(pid).split('\x00') if value]
        if argv == expected:
            result.append(pid)
    return result


def parse_status(status):
    uid = re.search(r'(?m)^Uid:\s*(\d+)', status)
    tracer = re.search(r'(?m)^TracerPid:\s*(\d+)', status)
    state = re.search(r'(?m)^State:\s*([A-Za-z])', status)
    if not (uid and tracer and state):
        raise Blocked('target_status_parse_failed')
    return {'uid': int(uid.group(1)), 'tracer_pid': int(tracer.group(1)), 'state': state.group(1)}


def parse_start_ticks(raw, expected_pid):
    """Linux /proc stat field 22; comm may contain spaces or parentheses."""
    prefix = str(expected_pid) + ' ('
    end = raw.rfind(') ')
    if not raw.startswith(prefix) or end < len(prefix):
        raise Blocked('target_stat_identity_invalid')
    fields = raw[end + 2:].split()  # state is field 3
    if len(fields) <= 19 or not fields[19].isdigit():
        raise Blocked('target_stat_starttime_missing')
    return int(fields[19])


def same_process(before, after):
    return (isinstance(before, dict) and isinstance(after, dict) and
            before.get('uid') == after.get('uid') and before.get('start_ticks') is not None and
            before.get('start_ticks') == after.get('start_ticks'))


def all_threads_detached(before_uid, thread_snapshot):
    """No assumption of resume/freezer state; only stable, untraced threads."""
    if not isinstance(thread_snapshot, dict) or not thread_snapshot.get('stable'):
        return False
    threads = thread_snapshot.get('threads')
    return (isinstance(threads, list) and bool(threads) and
            all(isinstance(t, dict) and t.get('uid') == before_uid and t.get('tracer_pid') == 0 and
                t.get('state') in ('R', 'S', 'D', 'I') for t in threads))


def complete_capture(receipt):
    """A partial stack or failed/unverified detach never upgrades to PASS."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get('lldb'), dict):
        return False
    lldb = receipt.get('lldb', {})
    return (receipt.get('state') == 'CAPTURED' and lldb.get('exit_code') == 0 and
            lldb.get('thread_lines', 0) > 0 and lldb.get('frame_lines', 0) > 0 and
            receipt.get('detach') == 'REPORTED_BY_LLDB' and receipt.get('detach_verified') is True)


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb', type=Path, required=True)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--package', required=True)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--server-relative-path', default='cache/lldb-server')
    parser.add_argument('--server-sha256', required=True, help='SHA-256 of the exact executable already in app cache')
    parser.add_argument('--ndk-prebuilt', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--startup-timeout', type=int, default=12)
    parser.add_argument('--lldb-timeout', type=int, default=45)
    args = parser.parse_args()
    if not PACKAGE.fullmatch(args.package) or not SERIAL.fullmatch(args.serial): parser.error('own diagnostic package and safe serial required')
    if not SERVER.fullmatch(args.server_relative_path) or not SHA.fullmatch(args.server_sha256): parser.error('relative cache executable and lowercase SHA-256 required')
    if not (2 <= args.pid <= 2**31-1 and 2 <= args.startup_timeout <= 30 and 5 <= args.lldb_timeout <= 60): parser.error('invalid bounded target or timeout')
    output = args.out_dir.resolve(); base = BASE.resolve()
    if not output.is_relative_to(base) or output == base or output.exists(): parser.error('new output directory below build required')
    lldb = args.ndk_prebuilt / ('bin/lldb.exe' if os.name == 'nt' else 'bin/lldb')
    python_home = args.ndk_prebuilt / 'python3'
    if not args.adb.is_file() or not lldb.is_file() or not python_home.is_dir(): parser.error('existing adb and complete NDK LLDB runtime required')
    if os.name == 'nt' and not (python_home / 'python311.dll').is_file(): parser.error('NDK python311.dll required on Windows')
    output.mkdir(parents=True)
    socket = '/mgmem-lldb-' + secrets.token_hex(12)
    port = None
    server_process = None
    server_pid = None
    target_before = None
    receipt = {'schema_version': 1, 'scope': 'OWN_DEBUGGABLE_PID_PAUSED_FOR_DIAGNOSTIC_NOT_PERFORMANCE',
               'started_at_utc': now(), 'state': 'NOT_RUN', 'status': 'NOT_RUN',
               'package': args.package, 'pid': args.pid, 'socket': socket, 'host_forward_port': None,
               'server_relative_path': args.server_relative_path, 'expected_server_sha256': args.server_sha256,
               'lldb_exe_sha256': hashlib.sha256(lldb.read_bytes()).hexdigest(), 'events': [],
               'raw_logs': 'LOCAL_PRIVATE_ONLY', 'target_killed': False, 'target_force_stopped': False,
               'other_processes_touched': False, 'detach': 'NOT_ATTEMPTED', 'target_after': 'UNKNOWN'}

    def save():
        path = output / 'receipt.pending.json'
        with path.open('w', encoding='utf-8', newline='\n') as file:
            json.dump(receipt, file, ensure_ascii=False, indent=2); file.write('\n'); file.flush(); os.fsync(file.fileno())
        os.replace(path, output / 'receipt.json')

    def adb(command, timeout=12, acceptable=(0,), private=False):
        started = time.monotonic()
        try:
            done = subprocess.run([str(args.adb), '-s', args.serial, *command], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            receipt['events'].append({'op': command[:3], 'timed_out': True, 'remote_state': 'UNKNOWN'})
            save(); raise Blocked('adb_timeout_' + command[0]) from error
        receipt['events'].append({'op': command[:3], 'exit_code': done.returncode,
                                  'elapsed_seconds': time.monotonic() - started,
                                  'stdout_sha256': hashlib.sha256(done.stdout).hexdigest(),
                                  'stderr_sha256': hashlib.sha256(done.stderr).hexdigest(),
                                  'raw_output': 'IN_MEMORY_ONLY' if private else 'IN_MEMORY_ONLY'})
        save()
        if done.returncode not in acceptable: raise Blocked('adb_failed_' + '_'.join(command[:2]))
        return done.stdout.decode('utf-8', 'replace')

    def target_info():
        status = adb(['shell', 'run-as', args.package, 'cat', f'/proc/{args.pid}/status'], private=True)
        stat = adb(['shell', 'run-as', args.package, 'cat', f'/proc/{args.pid}/stat'], private=True)
        value = {**parse_status(status), 'start_ticks': parse_start_ticks(stat, args.pid)}
        if target_before is not None and not same_process(target_before, value):
            raise Blocked('target_pid_reused_or_identity_changed')
        return value

    def all_thread_info():
        def tids():
            text = adb(['shell', 'run-as', args.package, 'ls', '-1', f'/proc/{args.pid}/task'], private=True)
            values = text.split()
            if not values or len(values) > 256 or any(not item.isdigit() for item in values):
                raise Blocked('thread_inventory_missing_invalid_or_too_large')
            return sorted({int(item) for item in values})
        first = tids()
        if args.pid not in first: raise Blocked('main_thread_missing')
        rows = []
        for tid in first:
            text = adb(['shell', 'run-as', args.package, 'cat', f'/proc/{args.pid}/task/{tid}/status'], private=True)
            rows.append({'tid': tid, **parse_status(text)})
        last = tids()
        return {'stable': first == last, 'threads': rows, 'count_before': len(first), 'count_after': len(last),
                'scope': 'STABLE_SNAPSHOT_OF_TASK_TIDS_NO_FREEZER_OR_RUNNABILITY_CLAIM'}

    def own_server_pids():
        # Android ps may truncate ARGS or omit argv[0]. Enumerate by basename,
        # add the target's actual TracerPid, and inspect each exact /proc argv.
        # Other process data stays in memory; never trust ps command text for
        # either declaring an attached debugger absent or signalling a PID.
        data = adb(['shell', 'run-as', args.package, 'pidof', 'lldb-server'],
                   acceptable=(0, 1), private=True)
        target = target_info()
        def read_cmdline(pid):
            return adb(['shell', 'run-as', args.package, 'cat', f'/proc/{pid}/cmdline'],
                       acceptable=(0, 1), private=True)
        return exact_server_pids(data, target['tracer_pid'], args.pid, args.server_relative_path, socket, read_cmdline)

    def cleanup_forward():
        if port is None: return
        # Never remove an unrelated or subsequently reused forward.
        data = adb(['forward', '--list'], private=True)
        expected = [line for line in data.splitlines() if line.split() == [args.serial, f'tcp:{port}', f'localabstract:{socket}']]
        if len(expected) != 1:
            receipt['forward_cleanup'] = 'SKIPPED_MAPPING_NOT_EXACT'; return
        adb(['forward', '--remove', f'tcp:{port}'])
        receipt['forward_cleanup'] = 'REMOVED_EXACT_OWN_MAPPING'

    try:
        save()
        uid = int(adb(['shell', 'run-as', args.package, 'id', '-u']).strip())
        cmdline = adb(['shell', 'run-as', args.package, 'cat', f'/proc/{args.pid}/cmdline'], private=True).split('\x00')[0]
        if cmdline not in (args.package,) and not cmdline.startswith(args.package + ':'): raise Blocked('target_cmdline_not_own_package')
        target_before = target_info()
        if target_before['uid'] != uid or target_before['tracer_pid'] != 0 or target_before['state'] in ('T', 't', 'Z', 'X'):
            raise Blocked('target_not_running_untraced_own_uid')
        receipt['target_before'] = target_before
        threads_before = all_thread_info()
        receipt['threads_before'] = threads_before
        if not all_threads_detached(uid, threads_before): raise Blocked('target_threads_not_stably_untraced')
        # run-as and exact cache path prevent a shell-user service attached to an unrelated app.
        adb(['shell', 'run-as', args.package, 'test', '-x', './' + args.server_relative_path])
        not_link = adb(['shell', 'run-as', args.package, 'test', '-L', './' + args.server_relative_path], acceptable=(0, 1))
        if receipt['events'][-1]['exit_code'] == 0: raise Blocked('server_symlink_rejected')
        observed = adb(['shell', 'run-as', args.package, 'sha256sum', './' + args.server_relative_path]).split()
        if not observed or observed[0].lower() != args.server_sha256: raise Blocked('server_identity_mismatch')
        result = adb(['forward', 'tcp:0', 'localabstract:' + socket]).strip()
        if not re.fullmatch(r'[0-9]{1,5}', result) or not 1 <= int(result) <= 65535: raise Blocked('invalid_forward_port')
        port = int(result); receipt['host_forward_port'] = port; save()
        server_stdout = (output / 'server.stdout').open('wb')
        server_stderr = (output / 'server.stderr').open('wb')
        try:
            command = [str(args.adb), '-s', args.serial, 'shell', 'run-as', args.package,
                       './' + args.server_relative_path, 'gdbserver', 'unix-abstract://' + socket, '--attach', str(args.pid)]
            server_process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=server_stdout, stderr=server_stderr)
            receipt['server_launch'] = {'host_pid': server_process.pid, 'started_at_utc': now(), 'target_may_be_paused': True}
            save()
            deadline = time.monotonic() + args.startup_timeout
            while time.monotonic() < deadline:
                if server_process.poll() is not None: raise Blocked('lldb_server_exited_before_ready')
                candidates = own_server_pids()
                if len(candidates) > 1: raise Blocked('more_than_one_matching_server')
                if candidates: server_pid = candidates[0]
                # The /proc/net/unix contents may include other apps; only a boolean/hash is retained.
                listing = adb(['shell', 'cat', '/proc/net/unix'], private=True)
                if server_pid and ('@' + socket) in listing.split(): break
                time.sleep(0.2)
            else: raise Blocked('own_server_or_socket_not_observed')
            receipt['server_pid'] = server_pid; receipt['server_ready'] = True; save()
            env = dict(os.environ)
            env['PYTHONHOME'] = str(python_home)
            env['PATH'] = str(python_home) + os.pathsep + env.get('PATH', '')
            commands = ['settings set auto-confirm true', 'settings set target.load-script-from-symbol-file false',
                        'settings set plugin.process.gdb-remote.packet-timeout 8', f'gdb-remote 127.0.0.1:{port}',
                        'thread backtrace all', 'process detach --keep-stopped false', 'quit']
            argv = [str(lldb), '--batch', '--no-lldbinit']
            for cmd in commands: argv += ['-o', cmd]
            try:
                run = subprocess.run(argv, env=env, capture_output=True, timeout=args.lldb_timeout)
                out, err, code = run.stdout, run.stderr, run.returncode
            except subprocess.TimeoutExpired as error:
                out, err, code = error.stdout or b'', error.stderr or b'', None
                receipt['lldb_timeout'] = True
            (output / 'lldb.stdout').write_bytes(out); (output / 'lldb.stderr').write_bytes(err)
            body = out.decode('utf-8', 'replace') + err.decode('utf-8', 'replace')
            receipt['lldb'] = {'exit_code': code, 'stdout_sha256': hashlib.sha256(out).hexdigest(),
                               'stderr_sha256': hashlib.sha256(err).hexdigest(),
                               'thread_lines': len(re.findall(r'(?m)^\s*\*?\s*thread #\d+', body)),
                               'frame_lines': len(re.findall(r'(?m)^\s*\*?\s*frame #\d+', body)),
                               'reported_detach': bool(re.search(r'(?i)process\s+\d+\s+detached', body))}
            receipt['detach'] = 'REPORTED_BY_LLDB' if receipt['lldb']['reported_detach'] else 'NOT_CONFIRMED_BY_LLDB'
        finally:
            server_stdout.close(); server_stderr.close()
        receipt['state'] = 'CAPTURED' if receipt['lldb']['thread_lines'] and receipt['lldb']['frame_lines'] else 'NO_STACK_CAPTURED'
    except (Blocked, OSError, ValueError) as error:
        receipt['state'] = 'BLOCKED'; receipt['reason'] = str(error)
    finally:
        # Only a fresh live process with the precise server path, PID argument,
        # and random socket can be sent SIGTERM; never signal the target PID.
        if server_process is not None:
            try:
                candidates = own_server_pids()
                if len(candidates) == 1 and candidates[0] != args.pid and (server_pid is None or candidates[0] == server_pid):
                    observed = target_info()
                    if observed['tracer_pid'] not in (0, candidates[0]):
                        receipt['server_cleanup'] = 'SKIPPED_DIFFERENT_TRACER'
                    else:
                        # Re-read /proc immediately before signalling: a PID
                        # may have exited or been reused since the first scan.
                        rechecked = own_server_pids()
                        if rechecked != candidates:
                            receipt['server_cleanup'] = 'SKIPPED_SERVER_IDENTITY_CHANGED'
                        else:
                            adb(['shell', 'run-as', args.package, 'kill', '-TERM', str(candidates[0])])
                            receipt['server_cleanup'] = 'SIGTERM_EXACT_OWN_SERVER_ONLY'
                elif not candidates:
                    observed = target_info()
                    receipt['server_cleanup'] = ('NO_MATCHING_SERVER_BUT_TARGET_TRACED_UNKNOWN' if observed['tracer_pid']
                                                 else 'NO_MATCHING_SERVER_MAIN_TRACER_ZERO_THREADS_UNCHECKED')
                else:
                    receipt['server_cleanup'] = 'SKIPPED_AMBIGUOUS_SERVER_IDENTITY'
            except Exception as error:
                receipt['server_cleanup'] = 'UNKNOWN_' + type(error).__name__
            try:
                server_process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                # Close only the local adb transport; target state remains unknown.
                server_process.terminate()
                try: server_process.wait(timeout=4)
                except subprocess.TimeoutExpired: receipt['local_transport'] = 'STILL_ALIVE_NOT_FORCED'
        try: cleanup_forward()
        except Exception as error: receipt['forward_cleanup'] = 'UNKNOWN_' + type(error).__name__
        if target_before is not None:
            try:
                after = target_info(); receipt['target_after'] = after
                threads_after = all_thread_info(); receipt['threads_after'] = threads_after
                receipt['detach_verified'] = (same_process(target_before, after) and after['tracer_pid'] == 0 and
                    after['state'] not in ('T', 't', 'Z', 'X') and all_threads_detached(target_before['uid'], threads_after))
            except Exception as error:
                receipt['target_after'] = 'UNKNOWN_' + type(error).__name__; receipt['detach_verified'] = False
        receipt['status'] = 'PASS_STACK_AND_DETACH' if complete_capture(receipt) else 'INCOMPLETE_OR_BLOCKED'
        receipt['finished_at_utc'] = now(); save()
    print(json.dumps({'status': receipt['status'], 'state': receipt['state'],
                      'detach_verified': receipt.get('detach_verified', False), 'receipt': str(output / 'receipt.json')}))
    raise SystemExit(0 if receipt['status'] == 'PASS_STACK_AND_DETACH' else 2)


if __name__ == '__main__': main()
