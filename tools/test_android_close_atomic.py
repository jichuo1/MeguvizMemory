"""Cross-platform fault injection for diagnostic receipt publication.

Uses only synthetic files in a new ignored build directory; no ADB or device.
"""
from pathlib import Path
import hashlib
import json
import os
import secrets
import subprocess
import time
from unittest import mock
from run_android_close_round import (ReceiptPublicationError, cleanup_running_child,
    publication_failure_marker, safe_reason, write_receipt_atomic)

ROOT=Path(__file__).resolve().parents[1]

def expected(value):
    return (json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')

def main():
    out=ROOT/'build'/('android-close-atomic-regression-'+secrets.token_hex(6))
    out.mkdir(parents=True,exist_ok=False)
    target=out/'receipt.json';pending=out/'receipt.pending'
    old=b'{"old_complete":true}\n';target.write_bytes(old)
    new={'state':'RUNNING','iteration':47,'sample':['0','0']};new_bytes=expected(new)
    real_replace=os.replace;real_fsync=os.fsync;fsyncs=[];attempts=[]
    def sync(fd):
        fsyncs.append(1);return real_fsync(fd)
    def transient(source,destination):
        assert fsyncs and target.read_bytes()==old and pending.read_bytes()==new_bytes
        attempts.append(1)
        if len(attempts)<=2:raise PermissionError('synthetic transient reader hold')
        return real_replace(source,destination)
    with mock.patch('run_android_close_round.os.fsync',side_effect=sync),mock.patch('run_android_close_round.os.replace',side_effect=transient):
        write_receipt_atomic(target,new)
    assert len(attempts)==3 and target.read_bytes()==new_bytes and not pending.exists()
    stable=target.read_bytes();blocked={'state':'RUNNING','iteration':48,'sample':['0','0','0']};blocked_bytes=expected(blocked)
    failures=[]
    def persistent(source,destination):
        assert target.read_bytes()==stable and pending.read_bytes()==blocked_bytes
        failures.append(1);raise PermissionError('synthetic persistent reader hold')
    start=time.monotonic()
    try:
        with mock.patch('run_android_close_round.os.replace',side_effect=persistent):write_receipt_atomic(target,blocked)
        raise AssertionError('persistent denial unexpectedly succeeded')
    except ReceiptPublicationError as error:
        assert str(error)=='HOST_RECEIPT_ATOMIC_REPLACE_PERMISSION_DENIED'
    elapsed=time.monotonic()-start
    assert .80<=elapsed<2.5 and len(failures)>=2,(elapsed,len(failures))
    assert target.read_bytes()==stable and pending.read_bytes()==blocked_bytes
    try:
        write_receipt_atomic(target,{'must_not_overwrite_pending':True})
        raise AssertionError('existing pending was overwritten')
    except ReceiptPublicationError as error:assert str(error)=='HOST_RECEIPT_PENDING_ALREADY_EXISTS'
    assert target.read_bytes()==stable and pending.read_bytes()==blocked_bytes
    # Receipt publication remains blocked. Cleanup must still issue the exact
    # own-app stop and wait for its transport before generating a sidecar.
    calls=[]
    class Child:
        def __init__(self,timeout=False):self.timeout=timeout
        def wait(self,timeout):
            calls.append(('wait',timeout))
            if self.timeout:raise subprocess.TimeoutExpired('private-process-command',timeout)
            return 0
    def stop_ok():calls.append(('stop',));return {'exit_code':0,'timed_out':False,'output_has_error':False}
    cleanup=cleanup_running_child(Child(),stop_ok)
    assert calls==[('stop',),('wait',15)] and cleanup['state']=='OWN_STOP_RETURNED_TRANSPORT_EXITED'
    assert cleanup['remote_process_state']=='OWN_PROCESS_ABSENCE_NOT_VERIFIED'
    receipt={'reason':'HOST_RECEIPT_ATOMIC_REPLACE_PERMISSION_DENIED','device_serial_sha256':'a'*64,'source_sha256':'b'*64,
             'cgroup_samples':[{},{}],'own_error_cleanup':cleanup['state'],'remote_process_state':cleanup['remote_process_state']}
    marker=publication_failure_marker(receipt,out,ReceiptPublicationError(receipt['reason']))
    assert marker['state']=='FAILED_OR_BLOCKED' and marker['normal_close_proven'] is False
    assert marker['last_complete_sha256']==hashlib.sha256(stable).hexdigest()
    assert marker['pending_sha256']==hashlib.sha256(blocked_bytes).hexdigest() and target.read_bytes()==stable and pending.read_bytes()==blocked_bytes
    calls.clear();timed=cleanup_running_child(Child(timeout=True),stop_ok)
    assert calls==[('stop',),('wait',15)] and timed['remote_process_state']=='UNKNOWN' and timed['state']=='OWN_STOP_RETURNED_TRANSPORT_TIMEOUT'
    calls.clear()
    def stop_denied():calls.append(('stop',));return {'exit_code':1,'timed_out':False}
    denied=cleanup_running_child(Child(),stop_denied)
    assert calls==[('stop',),('wait',15)] and denied['state']=='OWN_STOP_FAILED_TRANSPORT_EXITED' and denied['remote_process_state']=='UNKNOWN'
    raw=PermissionError(13,'private path',r'C:\Users\private\receipt.json')
    assert safe_reason(raw)=='HOST_PERMISSION_DENIED' and safe_reason(RuntimeError('bounded_failure'))=='bounded_failure'
    assert 'Users' not in safe_reason(RuntimeError('C:/Users/private/receipt.json'))
    print(json.dumps({'scope':'CROSS_PLATFORM_SYNTHETIC_IO_NO_DEVICE','state':'PASS',
        'transient_permission_errors':2,'transient_attempts':len(attempts),'fsync_before_replace':True,
        'persistent_elapsed_seconds':elapsed,'persistent_attempts':len(failures),'last_complete_preserved':True,
        'pending_preserved_and_not_overwritten':True,'error_path_redacted':True,'own_cleanup_before_sidecar':True,
        'cleanup_timeout_and_denial_unknown':True,
        'pending_sha256':hashlib.sha256(pending.read_bytes()).hexdigest()}))

if __name__=='__main__':main()
