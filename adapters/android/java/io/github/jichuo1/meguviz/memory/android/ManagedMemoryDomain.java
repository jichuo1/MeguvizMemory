package io.github.jichuo1.meguviz.memory.android;

import java.util.concurrent.atomic.AtomicLong;

/** Optional managed owner for the existing Memory core and bounded byte leases.
 * No native address or direct ByteBuffer is exposed. Explicitly close owners,
 * leases and the domain; GC is not a cleanup mechanism. */
public final class ManagedMemoryDomain implements AutoCloseable {
    static { System.loadLibrary("mgmem_pressure_jni"); }

    public static final int OPEN = 0;
    public static final int CLOSING = 1;
    public static final int CLOSED = 2;
    public static final int QUARANTINED = 3;
    public static final int MAX_CLEANUP_WORKERS = 32;
    public static final int MAX_MANAGED_DOMAINS = 1024;
    public static final int MAX_MANAGED_ALLOCATIONS = 65536;
    public static final int MAX_MANAGED_LEASES = 65536;
    public static final int MAX_CLOSE_GUARDS = 65536;
    /** Managed-runtime admission statuses, distinct from core backing budget. */
    public static final int STATUS_DOMAIN_LIMIT = 256;
    public static final int STATUS_ALLOCATION_LIMIT = 257;
    public static final int STATUS_LEASE_LIMIT = 258;
    public static final int STATUS_WORKER_LIMIT = 259;
    public static final int STATUS_CLOSE_GUARD_LIMIT = 260;
    private static final AtomicLong JAVA_QUARANTINE_SIGNALS = new AtomicLong();

    private final long token;
    private volatile boolean forgotten;
    private volatile boolean javaQuarantined;
    private int javaFailure = -1;
    private boolean forgetting;
    private boolean tokenDropped;
    private boolean javaObservedClosed;
    private int javaReportsInFlight;

    private ManagedMemoryDomain(boolean arena, long budget, long chunk) {
        if (budget <= 0 || (arena && chunk <= 0)) throw new IllegalArgumentException("positive budget and arena chunk required");
        final long result;
        try { result = nativeCreate(arena, budget, chunk); }
        catch (Throwable error) {
            recordJavaQuarantineSignal();
            throw propagate(error);
        }
        if (result <= 0) {
            if (result == -9) recordJavaQuarantineSignal();
            throw failure("create domain", -result);
        }
        token = result;
        try {
            int status = nativeClaimDomain(token);
            if (status != 0) {
                quarantineAfterUnknown(status);
                throw failure("claim domain", status);
            }
        } catch (Throwable error) {
            quarantineAfterUnknown(-1);
            throw propagate(error);
        }
    }

    public static ManagedMemoryDomain system(long budgetBytes) {
        return new ManagedMemoryDomain(false, budgetBytes, 0);
    }

    public static ManagedMemoryDomain arena(long budgetBytes, long chunkBytes) {
        return new ManagedMemoryDomain(true, budgetBytes, chunkBytes);
    }

    public static final class ManagedException extends IllegalStateException {
        private final int status;
        private ManagedException(String operation, long status) {
            super(operation + " failed: " + status);
            this.status = status >= 0 && status <= Integer.MAX_VALUE ? (int)status : -1;
        }
        public int status() { return status; }
    }

    private static ManagedException failure(String operation, long status) {
        return new ManagedException(operation, status);
    }

    private static RuntimeException propagate(Throwable error) {
        if (error instanceof Error) throw (Error)error;
        if (error instanceof RuntimeException) return (RuntimeException)error;
        return new IllegalStateException("unexpected checked JNI exception", error);
    }

    private static void suppress(Throwable original, Throwable secondary) {
        try { original.addSuppressed(secondary); } catch (Throwable ignored) { /* keep original error */ }
    }

    private void requireRemembered() {
        if (javaQuarantined) throw failure("domain quarantined", javaFailure);
        if (forgotten) throw new IllegalStateException("domain observation has been forgotten");
    }

    private void quarantineAfterUnknown(int status) {
        synchronized (this) {
            if (!javaQuarantined) {
                javaFailure = status;
                recordJavaQuarantineSignal();
                javaQuarantined = true;
            }
        }
        try { nativeQuarantine(token); } catch (Throwable ignored) { /* Java state remains quarantined */ }
    }

    private void enterJavaReport() {
        synchronized (this) {
            if (javaQuarantined) throw failure("domain quarantined", javaFailure);
            if (forgotten) throw new IllegalStateException("domain observation has been forgotten");
            if (javaObservedClosed) throw failure("domain closed", 7);
            if (javaReportsInFlight >= MAX_CLOSE_GUARDS) throw failure("Java report admission", STATUS_CLOSE_GUARD_LIMIT);
            ++javaReportsInFlight;
        }
    }

    private void leaveJavaReport() {
        boolean underflow = false;
        synchronized (this) {
            if (javaReportsInFlight == 0) underflow = true;
            else --javaReportsInFlight;
        }
        if (underflow) quarantineAfterUnknown(-1);
    }

    /** Per-wrapper mutating calls still reporting; not a native stack or RSS count. */
    public synchronized int pendingManagedOperations() { return javaReportsInFlight; }

    private static void recordJavaQuarantineSignal() {
        long value = JAVA_QUARANTINE_SIGNALS.get();
        while (value != Long.MAX_VALUE && !JAVA_QUARANTINE_SIGNALS.compareAndSet(value, value + 1))
            value = JAVA_QUARANTINE_SIGNALS.get();
    }

    /** Saturating, per-classloader count of conservative Java quarantine signals.
     * It is not a count of live resources, process memory, or recoverable tokens. */
    public static long javaQuarantineSignals() { return JAVA_QUARANTINE_SIGNALS.get(); }

    /** Allocated bytes are synchronously allocated and initialized to zero before
     * publication. Run on a suitable worker; no hard wall-clock bound is promised. */
    public Allocation allocate(long bytes, long alignment) {
        requireRemembered();
        if (bytes <= 0 || alignment <= 0 || (alignment & (alignment - 1)) != 0)
            throw new IllegalArgumentException("positive size and power-of-two alignment required");
        // Allocate the Java wrapper first; Java OOM cannot orphan a native owner.
        Allocation result = new Allocation(this);
        enterJavaReport();
        try {
            final long value;
            try { value = nativeAllocate(token, bytes, alignment); }
            catch (Throwable error) {
                quarantineAfterUnknown(-1);
                throw propagate(error);
            }
            if (value <= 0) {
                if (value == -9) quarantineAfterUnknown(9);
                throw failure("allocate", -value);
            }
            result.token = value;
            result.length = bytes;
            return result;
        } finally { leaveJavaReport(); }
    }

    /** Changes the soft admission limit; existing owners and leases survive. */
    public void setBudget(long bytes) {
        requireRemembered();
        if (bytes < 0) throw new IllegalArgumentException("budget must be non-negative");
        enterJavaReport();
        try {
            final int status;
            try { status = nativeSetBudget(token, bytes); }
            catch (Throwable error) {
                quarantineAfterUnknown(-1);
                throw propagate(error);
            }
            if (status != 0) throw failure("set budget", status);
        } finally { leaveJavaReport(); }
    }

    /** Stops admission and schedules quiet cleanup, without waiting for leases.
     * If worker admission fails, the domain stays CLOSING; call again to retry. */
    public void beginClose() {
        synchronized (this) {
            if (javaQuarantined) throw failure("domain quarantined", javaFailure);
            if (forgotten || javaObservedClosed) return;
        }
        try { enterJavaReport(); }
        catch (IllegalStateException error) {
            synchronized (this) { if ((forgotten || javaObservedClosed) && !javaQuarantined) return; }
            throw error;
        }
        try {
            final int status;
            try { status = nativeBeginClose(token); }
            catch (Throwable error) {
                quarantineAfterUnknown(-1);
                throw propagate(error);
            }
            if (status != 0) {
                synchronized (this) {
                    if (javaQuarantined) throw failure("domain quarantined", javaFailure);
                    if (forgotten) return;
                }
                throw failure("begin close", status);
            }
        } finally { leaveJavaReport(); }
    }

    @Override public void close() { beginClose(); }

    /** Does not wait for cleanup, but takes short locks and is not hard realtime;
     * QUARANTINED is not a successful close. */
    public int state() {
        synchronized (this) { if (javaQuarantined) return QUARANTINED; if (forgotten || javaObservedClosed) return CLOSED; }
        int value = nativeState(token);
        synchronized (this) {
            if (javaQuarantined) return QUARANTINED;
            if (forgotten || javaObservedClosed) return CLOSED;
            if (value == CLOSED) {
                if (javaReportsInFlight != 0) return CLOSING;
                javaObservedClosed = true;
            }
            return value;
        }
    }

    /** Native error for QUARANTINED, or zero if no failure is known. */
    public int failureStatus() {
        synchronized (this) { if (javaQuarantined) return javaFailure; if (forgotten) return 0; }
        int value = nativeFailure(token);
        synchronized (this) { return javaQuarantined ? javaFailure : forgotten ? 0 : value; }
    }

    /** Releases only the closed domain's observation token. This does not join,
     * collect or invoke user callbacks, but takes short internal locks. A
     * concurrent repeated call may return before the first has finished. */
    public void forgetClosed() {
        synchronized (this) {
            if (javaQuarantined) throw failure("domain quarantined", javaFailure);
            if (javaReportsInFlight != 0) throw failure("managed operation still reporting", 6);
            if (tokenDropped || forgetting) return;
            forgetting = true;
        }
        try {
            if (!forgotten) {
                int status = nativePrepareForget(token);
                if (status != 0) throw failure("prepare forget domain", status);
                // Publish the known CLOSED result before deleting its native
                // observation token. Concurrent state() never sees a stale ID.
                synchronized (this) {
                    if (javaQuarantined) throw failure("domain quarantined", javaFailure);
                    if (javaReportsInFlight != 0) throw failure("managed operation still reporting", 6);
                    forgotten = true;
                }
            }
            int status = nativeDropClosed(token);
            // If an earlier JNI return was interrupted after deletion, retrying
            // only this metadata drop is safe because CLOSED was already known.
            if (status != 0 && status != 5) throw failure("drop domain observation", status);
            synchronized (this) { tokenDropped = true; }
        } finally {
            synchronized (this) { forgetting = false; }
        }
    }

    /** [managedState,coreState,budget,backing,reserved,peak,live,retiring,
     * activeAllocations,activeLeases]. Active allocations include retiring
     * allocations whose owner was released but still have leases. The managed
     * and core state fields are sampled separately, not atomically.
     * Null means unavailable for an unspecified reason; -1 means outside Java's
     * signed range. Neither means zero or proves an API call stack has exited.
     * A concurrent forget may make this return null or throw. */
    public long[] snapshot() {
        synchronized (this) {
            if (javaQuarantined || javaReportsInFlight != 0) return null;
            if (forgotten) throw new IllegalStateException("domain observation has been forgotten");
        }
        long[] result = nativeSnapshot(token);
        synchronized (this) { return javaQuarantined || javaReportsInFlight != 0 || forgotten ? null : result; }
    }

    /** Current worker admission slots in this loaded runtime, not OS thread count
     * or evidence that JNI/core libraries may be unloaded. */
    public static int activeCleanupWorkers() { return nativeWorkerCount(); }

    /** Per loaded JNI runtime: [domains,owners,leasePins,workers,unclaimedDomains,
     * quarantinedDomains,closeGuards].
     * Fields are sampled separately. Entries can be in creation, retiring or
     * quarantine, so none is a precise live-owner count, process RSS, or proof
     * that JNI/core libraries may be unloaded. */
    public static long[] runtimeCounts() { return nativeRuntimeCounts(); }

    public static final class Allocation implements AutoCloseable {
        private final ManagedMemoryDomain domain;
        private long token;
        private long length;
        private Allocation(ManagedMemoryDomain owner) { domain = owner; }
        public synchronized long length() { return length; }

        public Lease borrowRead(long offset, long bytes) { return borrow(offset, bytes, false); }
        public Lease borrowWrite(long offset, long bytes) { return borrow(offset, bytes, true); }

        private Lease borrow(long offset, long bytes, boolean writable) {
            final long owner;
            synchronized (this) {
                if (token == 0) throw new IllegalStateException("allocation closed");
                if (offset < 0 || bytes <= 0 || offset > length || bytes > length - offset)
                    throw new IndexOutOfBoundsException("allocation range");
                owner = token;
            }
            // Reserve the outer Java owner before the native token is admitted;
            // OOM creating this wrapper cannot orphan a claimed lease.
            Lease result = new Lease(domain);
            domain.enterJavaReport();
            try {
            final long registration;
            try { registration = nativeBorrow(owner, offset, bytes, writable); }
            catch (Throwable error) {
                // The adapter may have registered a lease before Java observed
                // an exception. Its pre-reserved pin must remain intact.
                domain.quarantineAfterUnknown(-1);
                throw propagate(error);
            }
            if (registration <= 0) {
                if (registration == -9) domain.quarantineAfterUnknown(9);
                throw failure("borrow", -registration);
            }
            try {
                result.delegate = NativeMemoryLease.fromNativeRegistration(registration);
                return result;
            } catch (Throwable error) {
                // Protect the whole cancel call, not only its inner core call:
                // the adapter can still report an error after dropping its pin.
                long guard = 0;
                boolean cancelFailed = true;
                try {
                        guard = nativeGuardEnter(domain.token);
                        if (guard <= 0) {
                            domain.quarantineAfterUnknown((int)-guard);
                            suppress(error, failure("cancel guard admission", -guard));
                        } else {
                            int cancelled = nativeCancel(registration);
                            if (cancelled == 0) cancelFailed = false;
                            else {
                                domain.quarantineAfterUnknown(cancelled);
                                suppress(error, failure("cancel uncertain registration", cancelled));
                            }
                        }
                } catch (Throwable cleanup) {
                    domain.quarantineAfterUnknown(-1);
                    suppress(error, cleanup);
                }
                if (guard > 0) {
                        try {
                            int status = nativeGuardExit(guard, cancelFailed);
                            if (status != 0) {
                                domain.quarantineAfterUnknown(status);
                                suppress(error, failure("cancel guard release", status));
                            }
                        } catch (Throwable cleanup) {
                            domain.quarantineAfterUnknown(-1);
                            suppress(error, cleanup);
                        }
                    }
                throw propagate(error);
            }
            } finally { domain.leaveJavaReport(); }
        }

        /** May synchronously free backing. Arrange release on a cleanup thread. */
        @Override public void close() {
            synchronized (this) { if (token == 0) return; }
            try { domain.enterJavaReport(); }
            catch (IllegalStateException error) {
                synchronized (this) { if (token == 0) return; }
                throw error;
            }
            try {
                final long owner;
                synchronized (this) {
                    if (token == 0) return;
                    owner = token; token = 0; length = 0;
                }
                final int status;
                try { status = nativeRelease(owner); }
                catch (Throwable error) {
                    domain.quarantineAfterUnknown(-1);
                    throw propagate(error);
                }
                if (status != 0) {
                    domain.quarantineAfterUnknown(status);
                    throw failure("release owner; domain quarantined", status);
                }
            } finally { domain.leaveJavaReport(); }
        }
    }

    /** Managed view of one existing byte lease. The underlying registration is
     * deliberately not exposed, so a close failure can quarantine the domain. */
    public static final class Lease implements AutoCloseable {
        private final ManagedMemoryDomain domain;
        private volatile NativeMemoryLease delegate;
        private volatile int closeState = NativeMemoryLease.CLOSE_OPEN;
        private int closeFailure = NativeMemoryLease.CLOSE_FAILURE_UNKNOWN;
        private Lease(ManagedMemoryDomain domain) { this.domain = domain; }

        public long length() { return delegate.length(); }
        public boolean isWritable() { return delegate.isWritable(); }
        public int closeState() { return closeState; }
        public int closeFailureStatus() {
            return closeState == NativeMemoryLease.CLOSE_FAILED ? closeFailure : NativeMemoryLease.CLOSE_FAILURE_UNKNOWN;
        }
        public void read(long offset, byte[] destination, int arrayOffset, int count) {
            if (closeState != NativeMemoryLease.CLOSE_OPEN) throw new IllegalStateException("lease closing or closed");
            delegate.read(offset, destination, arrayOffset, count);
        }
        public void write(long offset, byte[] source, int arrayOffset, int count) {
            if (closeState != NativeMemoryLease.CLOSE_OPEN) throw new IllegalStateException("lease closing or closed");
            delegate.write(offset, source, arrayOffset, count);
        }
        @Override public void close() {
            synchronized (this) {
                if (closeState != NativeMemoryLease.CLOSE_OPEN) return;
                closeState = NativeMemoryLease.CLOSE_RELEASING;
            }
            boolean counted = false;
            try {
                try { domain.enterJavaReport(); counted = true; }
                catch (ManagedException error) {
                    if (error.status() == STATUS_CLOSE_GUARD_LIMIT) closeState = NativeMemoryLease.CLOSE_OPEN;
                    else { domain.quarantineAfterUnknown(error.status()); closeFailure = error.status(); closeState = NativeMemoryLease.CLOSE_FAILED; }
                    throw error;
                }
                final long guard;
                try { guard = nativeGuardEnter(domain.token); }
                catch (Throwable error) {
                    domain.quarantineAfterUnknown(-1);
                    closeFailure = -1; closeState = NativeMemoryLease.CLOSE_FAILED;
                    throw propagate(error);
                }
                if (guard <= 0) {
                    int status = (int)-guard;
                    if (status == STATUS_CLOSE_GUARD_LIMIT || status == 3 || status == 2) {
                        // Known failure before guard admission: no native release
                        // was attempted, so the same lease may be retried.
                        closeState = NativeMemoryLease.CLOSE_OPEN;
                    } else {
                        domain.quarantineAfterUnknown(status);
                        closeFailure = status; closeState = NativeMemoryLease.CLOSE_FAILED;
                    }
                    throw failure("lease close guard admission", status);
                }
                Throwable problem = null;
                int status = -1;
                try {
                    delegate.close();
                    if (delegate.closeState() == NativeMemoryLease.CLOSE_FAILED)
                        throw failure("native lease close", delegate.closeFailureStatus());
                } catch (Throwable error) {
                    problem = error;
                    status = delegate.closeFailureStatus();
                    domain.quarantineAfterUnknown(status);
                }
                try {
                    int result = nativeGuardExit(guard, problem != null);
                    if (result != 0) {
                        domain.quarantineAfterUnknown(result);
                        Throwable error = failure("lease close guard release", result);
                        if (problem == null) { problem = error; status = result; }
                        else suppress(problem, error);
                    }
                } catch (Throwable error) {
                    domain.quarantineAfterUnknown(-1);
                    if (problem == null) problem = error; else suppress(problem, error);
                }
                closeFailure = problem == null ? NativeMemoryLease.CLOSE_FAILURE_UNKNOWN : status;
                closeState = problem == null ? NativeMemoryLease.CLOSE_RELEASED : NativeMemoryLease.CLOSE_FAILED;
                if (problem != null) throw propagate(problem);
            } finally {
                if (closeState == NativeMemoryLease.CLOSE_RELEASING) {
                    domain.quarantineAfterUnknown(-1);
                    closeFailure = -1; closeState = NativeMemoryLease.CLOSE_FAILED;
                }
                if (counted) domain.leaveJavaReport();
            }
        }
    }

    private static native long nativeCreate(boolean arena, long budget, long chunk);
    private static native int nativeClaimDomain(long domain);
    private static native long nativeAllocate(long domain, long bytes, long alignment);
    private static native int nativeSetBudget(long domain, long bytes);
    private static native long nativeBorrow(long owner, long offset, long bytes, boolean writable);
    private static native int nativeCancel(long registration);
    private static native long nativeGuardEnter(long domain);
    private static native int nativeGuardExit(long guard, boolean failedClose);
    private static native int nativeRelease(long owner);
    private static native int nativeBeginClose(long domain);
    private static native int nativeState(long domain);
    private static native int nativeFailure(long domain);
    private static native int nativePrepareForget(long domain);
    private static native int nativeDropClosed(long domain);
    private static native long[] nativeSnapshot(long domain);
    private static native void nativeQuarantine(long domain);
    private static native int nativeWorkerCount();
    private static native long[] nativeRuntimeCounts();
}
