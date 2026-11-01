package io.github.jichuo1.meguviz.memory.android;

import android.app.Instrumentation;
import android.os.Bundle;
import android.os.Looper;
import java.util.Arrays;
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

    @Override public void onStart() {
        Bundle results=new Bundle();
        try {
            check(Looper.myLooper()!=Looper.getMainLooper(),"worker thread required");
            runCase(false);runCase(true);
            JSONObject report=new JSONObject();
            report.put("suite","android-native-borrow-v1");report.put("status","PASS");
            report.put("checks",checks);report.put("read_only_and_write_cases",2);
            report.put("initialization_assertion_and_bytes","PASS");
            report.put("owner_release_and_domain_closing_lease_access","PASS");
            report.put("duplicate_claim_and_idempotent_close","PASS");
            report.put("final_domain_backing_bytes",0);report.put("performance_qualified",false);
            report.put("scope","isolated_own_domain_no_BIL_no_actual_low_memory");
            results.putString("report_json",report.toString());finish(0,results);
        } catch(Throwable error) {
            results.putString("report_json","{\"suite\":\"android-native-borrow-v1\",\"status\":\"FAIL\",\"checks\":"+checks+",\"error_type\":\""+error.getClass().getSimpleName()+"\"}");
            finish(1,results);
        }
    }
}
