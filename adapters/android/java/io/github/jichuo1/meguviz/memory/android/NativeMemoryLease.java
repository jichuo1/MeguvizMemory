package io.github.jichuo1.meguviz.memory.android;

import java.util.Objects;

/** Bounded access to a native Memory lease registered by trusted native code.
 * No native pointer or direct buffer escapes. Close may synchronously run the
 * original owner/provider callback; use a suitable cleanup thread. */
public final class NativeMemoryLease implements AutoCloseable {
    static { System.loadLibrary("mgmem_pressure_jni"); }
    /** Maximum bytes in one write; separately invoked writes commit independently. */
    public static final int MAX_WRITE_BYTES = 1024 * 1024;
    /** Aggregate native staging payload limit across this loaded JNI runtime. */
    public static final int MAX_RUNTIME_STAGING_BYTES = 16 * 1024 * 1024;
    public static final int CLOSE_OPEN = 0;
    public static final int CLOSE_RELEASING = 1;
    public static final int CLOSE_RELEASED = 2;
    public static final int CLOSE_FAILED = 3;
    /** No native status was returned, or no failed close has completed. */
    public static final int CLOSE_FAILURE_UNKNOWN = -1;
    private long token;
    private final long length;
    private final boolean writable;
    private volatile int closeState = CLOSE_OPEN;
    private int closeFailure = CLOSE_FAILURE_UNKNOWN;

    private NativeMemoryLease(long registration) {
        if (registration <= 0) throw new IllegalArgumentException("invalid registration");
        long descriptor = nativeClaim(registration);
        if (descriptor == 0) throw new IllegalStateException("registration unavailable or already claimed");
        token = registration;
        writable = descriptor < 0;
        length = writable ? -descriptor : descriptor;
    }

    /** Consumes a registration returned by mgmem_android_borrow_register_v1.
     * If object construction fails before claim, native code still owns the
     * unclaimed registration and must cancel it. IDs are not security tokens. */
    public static NativeMemoryLease fromNativeRegistration(long registration) {
        return new NativeMemoryLease(registration);
    }

    public long length() { return length; }
    public boolean isWritable() { return writable; }

    /** A non-waiting observation of this wrapper's registration, not its domain. */
    public int closeState() { return closeState; }

    /** Meaningful only when closeState() is CLOSE_FAILED. A negative value
     * means no native status was returned, so the release outcome is unknown. */
    public int closeFailureStatus() {
        return closeState == CLOSE_FAILED ? closeFailure : CLOSE_FAILURE_UNKNOWN;
    }

    private void range(long offset, byte[] array, int arrayOffset, int count) {
        Objects.requireNonNull(array, "array");
        if (offset < 0 || count < 0 || offset > length || count > length - offset ||
                arrayOffset < 0 || arrayOffset > array.length || count > array.length - arrayOffset)
            throw new IndexOutOfBoundsException("lease or array range");
        if (token == 0) throw new IllegalStateException("lease closed");
    }

    /** Copies count bytes from this lease to an existing Java byte array.
     * If JNI fails, the destination array may already have been modified. */
    public synchronized void read(long offset, byte[] destination, int arrayOffset, int count) {
        range(offset, destination, arrayOffset, count);
        int status = nativeCopy(token, offset, destination, arrayOffset, count, false);
        if (status != 0) throw new IllegalStateException("native read failed: " + status);
    }

    /** Copies at most MAX_WRITE_BYTES to this exclusive WRITE lease. Staging
     * admission/allocation failures and JNI exceptions leave native bytes
     * unchanged. Separate calls commit separately; this does not promise
     * crash-atomicity or atomic observation by aliases. */
    public synchronized void write(long offset, byte[] source, int arrayOffset, int count) {
        range(offset, source, arrayOffset, count);
        if (!writable) throw new IllegalStateException("read-only lease");
        if (count > MAX_WRITE_BYTES) throw new IllegalArgumentException("write exceeds single-call limit");
        int status = nativeCopy(token, offset, source, arrayOffset, count, true);
        if (status == 3) throw new OutOfMemoryError("native staging allocation failed");
        if (status == 4) throw new IllegalStateException("native staging budget exhausted");
        if (status != 0) throw new IllegalStateException("native write failed: " + status);
    }

    /** Stops admission once and releases the lease outside this object's lock.
     * A concurrent repeated close may return before that release finishes; only
     * its first caller receives any error. A native failure is quarantined; a
     * JNI exception may leave the result unknown. Neither permits retry. */
    @Override public void close() {
        final long previous;
        synchronized (this) {
            if (token == 0) return;
            previous = token;
            token = 0;
            closeState = CLOSE_RELEASING;
        }
        int status = CLOSE_FAILURE_UNKNOWN;
        boolean released = false;
        try {
            status = nativeClose(previous);
            if (status != 0) throw new IllegalStateException("native close failed or unknown: " + status);
            released = true;
        } finally {
            // Publish the failure code before the volatile state transition.
            // JNI exceptions can arrive after the native handle was consumed.
            closeFailure = status == 0 ? CLOSE_FAILURE_UNKNOWN : status;
            closeState = released ? CLOSE_RELEASED : CLOSE_FAILED;
        }
    }

    private static native long nativeClaim(long registration);
    private static native int nativeCopy(long registration, long offset, byte[] array,
                                         int arrayOffset, int count, boolean write);
    private static native int nativeClose(long registration);
}
