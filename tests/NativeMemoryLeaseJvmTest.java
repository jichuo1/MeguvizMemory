package io.github.jichuo1.meguviz.memory.android;

import java.util.Arrays;

/** Runs against a host JVM and real core/JNI, not an Android-device test. */
public final class NativeMemoryLeaseJvmTest {
    static { System.loadLibrary("mgmem_pressure_jni"); }
    private static native long create(boolean writable);
    private static native boolean finish();
    private static void require(boolean value) { if (!value) throw new AssertionError(); }
    private static void rejected(Runnable call, Class<? extends Throwable> expected) {
        try { call.run(); } catch (Throwable error) {
            if (expected.isInstance(error)) return;
            throw new AssertionError(error);
        }
        throw new AssertionError("expected " + expected.getSimpleName());
    }
    public static void main(String[] unused) {
        final long token=create(false);require(token>0);
        NativeMemoryLease read=NativeMemoryLease.fromNativeRegistration(token);
        require(read.length()==16 && !read.isWritable());
        rejected(() -> NativeMemoryLease.fromNativeRegistration(token),IllegalStateException.class);
        byte[] output=new byte[16];read.read(0,output,0,output.length);
        byte[] expected=new byte[16];Arrays.fill(expected,(byte)0x5a);require(Arrays.equals(output,expected));
        rejected(() -> read.write(0,new byte[1],0,1),IllegalStateException.class);
        rejected(() -> read.read(Long.MAX_VALUE,output,0,1),IndexOutOfBoundsException.class);
        rejected(() -> read.read(0,output,Integer.MAX_VALUE,1),IndexOutOfBoundsException.class);
        read.read(16,output,16,0);
        read.close();read.close();
        rejected(() -> read.read(0,output,0,1),IllegalStateException.class);
        require(finish());

        long writeToken=create(true);require(writeToken>0);
        NativeMemoryLease write=NativeMemoryLease.fromNativeRegistration(writeToken);
        require(write.length()==16 && write.isWritable());
        byte[] input={1,2,3,4};write.write(3,input,0,input.length);
        byte[] actual=new byte[4];write.read(3,actual,0,4);require(Arrays.equals(actual,input));
        write.close();require(finish());
        System.out.println("borrow_jni_real_jvm: PASS");
    }
}
