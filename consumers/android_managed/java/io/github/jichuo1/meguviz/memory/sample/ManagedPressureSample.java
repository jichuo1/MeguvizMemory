package io.github.jichuo1.meguviz.memory.sample;

import android.content.Context;
import android.os.Looper;

import io.github.jichuo1.meguviz.memory.android.ManagedMemoryDomain;
import io.github.jichuo1.meguviz.memory.android.ManagedPressureBackend;
import io.github.jichuo1.meguviz.memory.android.MemoryPressureAdapter;
import io.github.jichuo1.meguviz.memory.android.NativeMemoryLease;

/** An opt-in application-side consumer; callers serialize all methods on a worker. */
public final class ManagedPressureSample {
    private static final long WAIT_NANOS = 5_000_000_000L;
    private static final int BUSY = 6;
    private final Context application;
    private ManagedMemoryDomain domain;
    private ManagedMemoryDomain.Pressure pressure;
    private ManagedPressureBackend backend;
    private MemoryPressureAdapter callbacks;
    private ManagedMemoryDomain.Allocation owner;
    private ManagedMemoryDomain.Lease lease;
    private boolean openAttempted;
    private boolean closing;
    private boolean closed;

    /** Create on a worker and keep this object until cleanup succeeds. */
    public ManagedPressureSample(Context context) {
        if (Looper.myLooper() == Looper.getMainLooper()) throw new IllegalStateException("worker required");
        if (context == null || context.getApplicationContext() == null)
            throw new IllegalArgumentException("application context required");
        application = context.getApplicationContext();
    }

    private void requireWorker() {
        if (Looper.myLooper() == Looper.getMainLooper()) throw new IllegalStateException("worker required");
    }

    /** Registers Android callbacks and creates a small, explicitly owned workspace.
     * If a step fails, retain this session and call closeOnWorker to attempt cleanup. */
    public void openOnWorker() {
        requireWorker();
        if (openAttempted || closing) throw new IllegalStateException("session already started");
        openAttempted = true;
        domain = ManagedMemoryDomain.arena(1024 * 1024, 65536);
        pressure = domain.attachPressure(512 * 1024, 0);
        backend = new ManagedPressureBackend(pressure);
        callbacks = new MemoryPressureAdapter(application, backend);
        owner = domain.allocate(4096, 64);
        lease = owner.borrowWrite(0, 4096);
        byte[] input = {1, 2, 3, 4};
        lease.write(0, input, 0, input.length);
        byte[] output = new byte[input.length];
        lease.read(0, output, 0, output.length);
        for (int i = 0; i < input.length; ++i)
            if (input[i] != output[i]) throw new IllegalStateException("workspace contents differ");
    }

    /** A direct synthetic request, not an Android low-memory observation. */
    public static final class EventObservation {
        public final long ticket;
        public final int state;
        public final int status;
        public final long applySequence;
        public final long frameworkForwarded;
        public final long frameworkRejected;
        private EventObservation(long ticket, int state, int status, long sequence, long forwarded, long rejected) {
            this.ticket = ticket; this.state = state; this.status = status; this.applySequence = sequence;
            this.frameworkForwarded = forwarded; this.frameworkRejected = rejected;
        }
    }
    /** State returned when the bounded wait or result query cannot establish an outcome. */
    public static final int EVENT_UNAVAILABLE = -1;
    /** State returned when the direct request was never accepted. */
    public static final int EVENT_REJECTED = -2;

    /** Submit one synthetic event and observe its ticket on the worker.
     * Only BUSY (known not admitted) is retried. An accepted ticket is never resubmitted. */
    public EventObservation submitSyntheticCriticalOnWorker() throws InterruptedException {
        requireWorker();
        if (closing || lease == null || pressure == null) throw new IllegalStateException("workspace not open");
        long deadline = System.nanoTime() + WAIT_NANOS;
        long ticket;
        do {
            ticket = pressure.submit(ManagedMemoryDomain.Pressure.CRITICAL);
            if (ticket != -BUSY) break;
            if (System.nanoTime() - deadline < 0) Thread.sleep(5);
        } while (System.nanoTime() - deadline < 0);
        if (ticket <= 0) return observation(0, EVENT_REJECTED, ticket == 0 ? 9 : (int)-ticket, 0);
        while (System.nanoTime() - deadline < 0) {
            long[] result = pressure.eventResult(ticket);
            if (result == null || result.length != 4 || result[2] != ticket)
                return observation(ticket, EVENT_UNAVAILABLE, -1, 0);
            if (result[0] < ManagedMemoryDomain.Pressure.EVENT_QUEUED ||
                    result[0] > ManagedMemoryDomain.Pressure.EVENT_EXPIRED ||
                    result[1] < 0 || result[1] > Integer.MAX_VALUE)
                return observation(ticket, EVENT_UNAVAILABLE, -1, 0);
            int state = (int)result[0];
            boolean applied = state == ManagedMemoryDomain.Pressure.EVENT_APPLIED;
            boolean error = state == ManagedMemoryDomain.Pressure.EVENT_FAILED ||
                    state == ManagedMemoryDomain.Pressure.EVENT_CANCELLED;
            if ((applied && (result[1] != 0 || result[3] <= 0)) ||
                    (error && (result[1] == 0 || result[3] != 0)) ||
                    (!applied && !error && (result[1] != 0 || result[3] != 0)))
                return observation(ticket, EVENT_UNAVAILABLE, -1, 0);
            if (state != ManagedMemoryDomain.Pressure.EVENT_QUEUED)
                return observation(ticket, state, (int)result[1], result[3]);
            Thread.sleep(5);
        }
        return observation(ticket, EVENT_UNAVAILABLE, -1, 0);
    }

    private EventObservation observation(long ticket, int state, int status, long sequence) {
        // These are separately sampled counters, not proof of actual low memory.
        return new EventObservation(ticket, state, status, sequence,
                callbacks == null ? 0 : callbacks.forwardedEvents(),
                backend == null ? 0 : backend.rejectedEvents());
    }

    /** Stops callbacks, returns this worker's lease and requests domain close.
     * Returns false on a bounded wait, never forces release; retain this session
     * and retry on a worker. Callers must serialize calls, including retries.
     * Exceptions and quarantine are not success. */
    public boolean closeOnWorker() throws InterruptedException {
        requireWorker();
        if (closed) return true;
        closing = true;
        if (callbacks != null) {
            callbacks.close();
            callbacks = null;
        } else if (pressure != null) {
            pressure.beginClose();
        }
        if (owner != null) {
            owner.close();
            owner = null;
        }
        if (lease != null) {
            int state = lease.closeState();
            if (state == NativeMemoryLease.CLOSE_OPEN) {
                lease.close();
                state = lease.closeState();
            }
            if (state == NativeMemoryLease.CLOSE_FAILED)
                throw new IllegalStateException("lease release failed: " + lease.closeFailureStatus());
            if (state != NativeMemoryLease.CLOSE_RELEASED) return false;
            lease = null;
        }
        if (domain == null) { closed = true; return true; }
        domain.beginClose();
        long deadline = System.nanoTime() + WAIT_NANOS;
        int state = domain.state();
        while (state == ManagedMemoryDomain.CLOSING && System.nanoTime() - deadline < 0) {
            Thread.sleep(5);
            state = domain.state();
        }
        if (state == ManagedMemoryDomain.QUARANTINED)
            throw new IllegalStateException("domain quarantined: " + domain.failureStatus());
        if (state != ManagedMemoryDomain.CLOSED) return false;
        long[] finalSnapshot = domain.snapshot();
        if (finalSnapshot == null || finalSnapshot.length != 10 || finalSnapshot[0] != ManagedMemoryDomain.CLOSED ||
                finalSnapshot[1] != 2 || finalSnapshot[3] != 0 || finalSnapshot[4] != 0 ||
                finalSnapshot[6] != 0 || finalSnapshot[7] != 0 || finalSnapshot[8] != 0 || finalSnapshot[9] != 0)
            throw new IllegalStateException("final domain accounting unavailable");
        domain.forgetClosed();
        domain = null; pressure = null; backend = null; closed = true;
        return true;
    }
}
