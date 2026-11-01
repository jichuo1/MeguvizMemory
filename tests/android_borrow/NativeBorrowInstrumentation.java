package io.github.jichuo1.meguviz.memory.android;

import android.app.Instrumentation;
import android.os.Bundle;
import android.os.Looper;
import java.util.Arrays;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import org.json.JSONObject;

/** Isolated native-domain and real Java/JNI contract probe; no production hook. */
public final class NativeBorrowInstrumentation extends Instrumentation {
    static { System.loadLibrary("mgmem_borrow_fixture"); }
    private static native long nativeCreate(boolean writable);
    private static native boolean nativeBeginClose();
    private static native long[] nativeSnapshot();
    private static native boolean nativeCollectBusy();
    private static native boolean nativeFinish();
    private static native void nativeCleanupBestEffort();
    private static native long nativeCreateFailedRelease();
    private static native boolean nativeReleaseGate(int action);
    private static native boolean nativeArmJavaException();
    private static native boolean nativeFailureQuarantined();
    private int checks;

    private void check(boolean condition,String label) {
        ++checks;
        if(!condition)throw new AssertionError(label);
    }
    private void rejected(Runnable action,Class<? extends Throwable> expected,String label) {
        try {action.run();}
        catch(Throwable error) {check(expected.isInstance(error),label);return;}
        throw new AssertionError(label);
    }
    @Override public void onCreate(Bundle arguments) {super.onCreate(arguments);start();}

    private void runCase(boolean writable) {
        NativeMemoryLease lease=null;
        try {
            long token=nativeCreate(writable);
            check(token>0,"registration and initialized flag");
            lease=NativeMemoryLease.fromNativeRegistration(token);
            final NativeMemoryLease active=lease;
            check(lease.length()==16 && lease.isWritable()==writable,"claim descriptor");
            check(lease.closeState()==NativeMemoryLease.CLOSE_OPEN &&
                lease.closeFailureStatus()==NativeMemoryLease.CLOSE_FAILURE_UNKNOWN,"initial close state");
            rejected(()->NativeMemoryLease.fromNativeRegistration(token),IllegalStateException.class,"single claim");
            byte[] read=new byte[16];lease.read(0,read,0,16);
            byte[] expected=new byte[16];Arrays.fill(expected,(byte)0x5a);
            check(Arrays.equals(read,expected),"initialized bytes");
            check(nativeBeginClose(),"release owner and begin domain close");
            long[] held=nativeSnapshot();
            check(held!=null && held.length==8 && held[0]==1 && held[1]>0 && held[2]==0 &&
                held[3]==0 && held[4]==32 && held[5]==1 && held[6]==1 && held[7]==2,"retiring lease and owner pin");
            check(nativeCollectBusy(),"collect waits for lease");
            Arrays.fill(read,(byte)0);lease.read(0,read,0,16);
            check(Arrays.equals(read,expected),"read after owner/domain close");
            if(writable) {
                byte[] patch={1,2,3,4};lease.write(5,patch,0,patch.length);
                byte[] copied=new byte[4];lease.read(5,copied,0,4);
                check(Arrays.equals(patch,copied),"write after owner/domain close");
            } else {
                rejected(()->active.write(0,new byte[1],0,1),IllegalStateException.class,"read only");
            }
            rejected(()->active.read(Long.MAX_VALUE,new byte[1],0,1),IndexOutOfBoundsException.class,"lease bounds");
            rejected(()->active.read(0,new byte[1],Integer.MAX_VALUE,1),IndexOutOfBoundsException.class,"array bounds");
            lease.read(16,read,16,0);
            lease.close();lease.close();
            check(lease.closeState()==NativeMemoryLease.CLOSE_RELEASED &&
                lease.closeFailureStatus()==NativeMemoryLease.CLOSE_FAILURE_UNKNOWN,"released close state");
            rejected(()->active.read(0,new byte[1],0,1),IllegalStateException.class,"closed lease");
            long[] released=nativeSnapshot();
            check(released!=null && released[0]==1 && released[1]==0 && released[2]==0 && released[3]==0 &&
                released[4]==0 && released[5]==0 && released[6]==0 && released[7]==1,"lease released and pin returned");
            check(nativeFinish(),"closed domain zero and destroy");
            lease=null;
        } finally {
            if(lease!=null)try {lease.close();} catch(Throwable ignored) { /* reported by primary failure */ }
            nativeCleanupBestEffort();
        }
    }

    private void runReleasingCase() throws InterruptedException {
        NativeMemoryLease lease=null;Thread closer=null,duplicate=null;
        AtomicReference<Throwable> closeError=new AtomicReference<>(),duplicateError=new AtomicReference<>();
        AtomicBoolean duplicateRejectedRead=new AtomicBoolean();
        try {
            long token=nativeCreate(false);check(token>0,"releasing registration");
            lease=NativeMemoryLease.fromNativeRegistration(token);final NativeMemoryLease active=lease;
            check(lease.closeState()==NativeMemoryLease.CLOSE_OPEN,"releasing initial open");
            check(nativeBeginClose(),"releasing domain close");
            check(nativeReleaseGate(0),"arm bounded release gate");
            closer=new Thread(()->{try {active.close();}catch(Throwable error){closeError.set(error);}},"borrow-close-first");
            closer.start();
            check(nativeReleaseGate(1),"release callback entered");
            check(lease.closeState()==NativeMemoryLease.CLOSE_RELEASING &&
                lease.closeFailureStatus()==NativeMemoryLease.CLOSE_FAILURE_UNKNOWN,"observable releasing state");
            duplicate=new Thread(()->{
                try {
                    active.close();
                    try {active.read(0,new byte[1],0,1);}
                    catch(IllegalStateException expected) {duplicateRejectedRead.set(true);}
                } catch(Throwable error) {duplicateError.set(error);}
            },"borrow-close-repeat");
            duplicate.start();duplicate.join(1500);
            check(!duplicate.isAlive() && duplicateError.get()==null && duplicateRejectedRead.get(),"repeat close does not wait");
            check(lease.closeState()==NativeMemoryLease.CLOSE_RELEASING,"repeat does not imply release completion");
            check(nativeReleaseGate(2),"allow release callback");
            closer.join(7000);
            check(!closer.isAlive() && closeError.get()==null && nativeReleaseGate(3),"first close completed before gate timeout");
            check(lease.closeState()==NativeMemoryLease.CLOSE_RELEASED &&
                lease.closeFailureStatus()==NativeMemoryLease.CLOSE_FAILURE_UNKNOWN,"observable released state");
            long[] snapshot=nativeSnapshot();
            check(snapshot!=null && snapshot[0]==1 && snapshot[1]==0 && snapshot[2]==0 &&
                snapshot[3]==0 && snapshot[4]==0 && snapshot[5]==0 && snapshot[6]==0 && snapshot[7]==1,"releasing case zero");
            check(nativeFinish(),"releasing case destroy");lease=null;
        } catch(InterruptedException error) {
            Thread.currentThread().interrupt();throw error;
        } finally {
            boolean interrupted=Thread.interrupted();
            nativeReleaseGate(2);
            try {if(closer!=null)closer.join(7000);}catch(InterruptedException error){interrupted=true;}
            try {if(duplicate!=null)duplicate.join(1500);}catch(InterruptedException error){interrupted=true;}
            if(lease!=null)try {lease.close();}catch(Throwable ignored) {}
            nativeCleanupBestEffort();
            if(interrupted)Thread.currentThread().interrupt();
        }
    }

    private void runJavaExceptionCase() {
        NativeMemoryLease lease=null;
        try {
            long token=nativeCreate(false);check(token>0,"exception registration");
            lease=NativeMemoryLease.fromNativeRegistration(token);final NativeMemoryLease active=lease;
            check(lease.closeState()==NativeMemoryLease.CLOSE_OPEN,"exception initial open");
            check(nativeBeginClose(),"exception domain close");
            check(nativeArmJavaException(),"arm test-only pending JNI exception");
            rejected(active::close,IllegalStateException.class,"pending JNI exception propagated");
            check(lease.closeState()==NativeMemoryLease.CLOSE_FAILED &&
                lease.closeFailureStatus()==NativeMemoryLease.CLOSE_FAILURE_UNKNOWN,"JNI exception result unknown");
            lease.close();check(lease.closeState()==NativeMemoryLease.CLOSE_FAILED,"exception close not retried");
            long[] snapshot=nativeSnapshot();
            check(snapshot!=null && snapshot[0]==1 && snapshot[1]==0 && snapshot[2]==0 &&
                snapshot[3]==0 && snapshot[4]==0 && snapshot[5]==0 && snapshot[6]==0 && snapshot[7]==1,"specific exception fixture pin released");
            check(nativeFinish(),"specific exception fixture destroy");lease=null;
        } finally {
            if(lease!=null)try {lease.close();}catch(Throwable ignored) {}
            nativeCleanupBestEffort();
        }
    }

    private void runCheckedFailureCase() {
        NativeMemoryLease lease=null;boolean quarantined=false;
        try {
            long token=nativeCreateFailedRelease();check(token>0,"checked failure registration");
            lease=NativeMemoryLease.fromNativeRegistration(token);final NativeMemoryLease active=lease;
            check(lease.closeState()==NativeMemoryLease.CLOSE_OPEN,"checked failure initial open");
            check(nativeBeginClose(),"checked failure domain close");
            rejected(active::close,IllegalStateException.class,"checked native error propagated");
            check(lease.closeState()==NativeMemoryLease.CLOSE_FAILED && lease.closeFailureStatus()==9,"checked failure status");
            lease.close();check(lease.closeState()==NativeMemoryLease.CLOSE_FAILED && lease.closeFailureStatus()==9,"checked failure not retried");
            check(nativeFailureQuarantined(),"checked failure retains claim, charge and owner pin");
            quarantined=true;lease=null; // This failing fixture is intentionally retained until process exit.
        } finally {
            if(lease!=null)try {lease.close();}catch(Throwable ignored) {}
            if(!quarantined)nativeCleanupBestEffort();
        }
    }

    @Override public void onStart() {
        Bundle results=new Bundle();
        try {
            check(Looper.myLooper()!=Looper.getMainLooper(),"worker thread required");
            runCase(false);runCase(true);runReleasingCase();runJavaExceptionCase();runCheckedFailureCase();
            JSONObject report=new JSONObject();
            report.put("suite","android-native-borrow-v2");report.put("status","PASS");
            report.put("checks",checks);report.put("read_only_and_write_cases",2);
            report.put("initialization_assertion_and_bytes","PASS");
            report.put("owner_release_and_domain_closing_lease_access","PASS");
            report.put("duplicate_claim_and_idempotent_close","PASS");
            report.put("close_state_open_releasing_released","PASS");
            report.put("close_state_pending_java_exception","PASS");
            report.put("close_state_checked_failure","PASS");
            report.put("pending_java_exception_native_status",NativeMemoryLease.CLOSE_FAILURE_UNKNOWN);
            report.put("checked_failure_native_status",9);
            report.put("normal_domains_final_backing_bytes",0);
            report.put("quarantined_failure_backing_bytes",32);report.put("quarantined_failure_owner_pins",2);
            report.put("quarantined_failure_reclaimed",false);report.put("performance_qualified",false);
            report.put("scope","isolated_normal_and_quarantined_domains_no_BIL_no_actual_low_memory");
            results.putString("report_json",report.toString());finish(0,results);
        } catch(Throwable error) {
            results.putString("report_json","{\"suite\":\"android-native-borrow-v2\",\"status\":\"FAIL\",\"checks\":"+checks+",\"error_type\":\""+error.getClass().getSimpleName()+"\"}");
            finish(1,results);
        }
    }
}
