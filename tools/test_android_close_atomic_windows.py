"""Verify real Windows sharing violations during receipt replacement.

The test uses only new synthetic files under ignored build/; no device, live
receipt, existing user file, or global filesystem setting is touched. The
instrumented replace forwards every call to the actual Windows implementation;
no failure is injected by a mock. A deliberately retained pending file remains
in the isolated directory after the persistent-lock case.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from unittest import mock

from run_android_close_round import ReceiptPublicationError, write_receipt_atomic

ROOT=Path(__file__).resolve().parents[1]

def encoded(value):
    return (json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')

class WindowsReaderWithoutDeleteShare:
    """An actual Win32 reader that explicitly withholds FILE_SHARE_DELETE."""
    def __init__(self,path):
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        create=kernel.CreateFileW
        create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,
                         wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        create.restype=wintypes.HANDLE
        close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
        # GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE (no DELETE), OPEN_EXISTING.
        handle=create(str(path.resolve()),0x80000000,0x00000001|0x00000002,None,3,0x80,None)
        if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        self.handle=handle;self._close=close
    def close(self):
        if self.handle is not None:
            handle=self.handle;self.handle=None
            if not self._close(handle):raise ctypes.WinError(ctypes.get_last_error())
    def __enter__(self):return self
    def __exit__(self,*_):self.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/android-close-investigation-20261030/atomic-windows-os-sharing.json')
    args=parser.parse_args();output=args.output.resolve()
    if not output.is_relative_to(ROOT/'artifacts/android-close-investigation-20261030') or output.exists():parser.error('new scoped output required')
    if os.name!='nt':
        result={'state':'SKIP_WINDOWS_ONLY','scope':'NO_DEVICE_NO_SHARED_FILE'}
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result));return
    work=ROOT/'build'/('android-close-windows-sharing-'+secrets.token_hex(6));work.mkdir(parents=True,exist_ok=False)
    target=work/'receipt.json';pending=work/'receipt.pending';old=b'{"complete":"old"}\n';target.write_bytes(old)
    candidate={'state':'RUNNING','iteration':47};new_bytes=encoded(candidate)
    real_replace=os.replace;attempts=[];sharing_seen=threading.Event();worker_error=[]
    def traced_replace(source,destination):
        try:result=real_replace(source,destination)
        except PermissionError as error:
            attempts.append({'kind':'permission','winerror':error.winerror});sharing_seen.set();raise
        attempts.append({'kind':'success'});return result
    def publish():
        try:write_receipt_atomic(target,candidate)
        except Exception as error:worker_error.append(error)
    started=time.monotonic();worker=threading.Thread(target=publish,name='own-receipt-writer')
    with mock.patch('run_android_close_round.os.replace',new=traced_replace):
        reader=WindowsReaderWithoutDeleteShare(target)
        try:
            worker.start()
            assert sharing_seen.wait(.7),'OS sharing violation did not occur before deadline'
            # MoveFileExW may report ACCESS_DENIED rather than SHARING_VIOLATION
            # when a destination reader withholds delete sharing. Both are real
            # kernel results; the lock and preserved files establish the cause.
            assert attempts[0]['winerror'] in (5,32,33),'expected actual Windows sharing denial'
            assert worker.is_alive() and target.read_bytes()==old and pending.read_bytes()==new_bytes
        finally:
            reader.close();worker.join(3)
        assert not worker.is_alive() and not worker_error,'retry did not finish safely'
    transient_elapsed=time.monotonic()-started
    assert len(attempts)>=2 and attempts[-1]['kind']=='success'
    assert target.read_bytes()==new_bytes and not pending.exists()
    transient={'real_sharing_violations':sum(x['kind']=='permission' for x in attempts),
               'first_winerror':attempts[0]['winerror'],'replace_attempts':len(attempts),
               'old_and_pending_complete_while_locked':True,'after_release_replacement_succeeded':True,
               'elapsed_seconds':transient_elapsed}

    # Withhold DELETE for the entire finite retry window; keep both files when
    # admission expires, and verify a second caller cannot overwrite pending.
    stable=target.read_bytes();blocked={'state':'RUNNING','iteration':48};blocked_bytes=encoded(blocked)
    attempts=[];started=time.monotonic()
    with mock.patch('run_android_close_round.os.replace',new=traced_replace):
        with WindowsReaderWithoutDeleteShare(target):
            try:write_receipt_atomic(target,blocked)
            except ReceiptPublicationError as error:
                assert str(error)=='HOST_RECEIPT_ATOMIC_REPLACE_PERMISSION_DENIED'
            else:raise AssertionError('persistent OS sharing violation unexpectedly succeeded')
            elapsed=time.monotonic()-started
            assert .80<=elapsed<3.0 and len(attempts)>=2 and all(x['winerror'] in (5,32,33) for x in attempts)
            assert target.read_bytes()==stable and pending.read_bytes()==blocked_bytes
            try:write_receipt_atomic(target,{'must_not_overwrite':True})
            except ReceiptPublicationError as error:assert str(error)=='HOST_RECEIPT_PENDING_ALREADY_EXISTS'
            else:raise AssertionError('existing pending successor was overwritten')
            assert target.read_bytes()==stable and pending.read_bytes()==blocked_bytes
    assert target.read_bytes()==stable and pending.read_bytes()==blocked_bytes
    result={'state':'PASS_REAL_WINDOWS_OS_SHARING','scope':'SYNTHETIC_OWN_FILES_ACTUAL_WIN32_SHARE_DENIAL_NO_DEVICE',
            'runner_source_sha256':hashlib.sha256((ROOT/'tools/run_android_close_round.py').read_bytes()).hexdigest(),
            'test_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'share_mode':'READ_WRITE_ALLOWED_DELETE_NOT_SHARED','transient':transient,
            'persistent':{'winerrors':sorted({x['winerror'] for x in attempts}),'attempts':len(attempts),'elapsed_seconds':elapsed,
                          'old_complete_preserved':True,'pending_complete_preserved':True,
                          'pending_not_overwritten_by_second_call':True,
                          'pending_sha256':hashlib.sha256(blocked_bytes).hexdigest()},
            'mechanism':'Real kernel32 CreateFileW handle and unmocked OS replace; wrapper only records actual return/errors',
            'limitations':['Synthetic files only, no current device receipt read','Not a device/runtime/performance test']}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
