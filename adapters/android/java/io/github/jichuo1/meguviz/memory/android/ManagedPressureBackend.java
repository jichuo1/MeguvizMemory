package io.github.jichuo1.meguviz.memory.android;

import android.os.Looper;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/** Framework callback bridge for an explicitly attached managed pressure
 * controller. Success from notify means queued, not applied. */
public final class ManagedPressureBackend implements MemoryPressureAdapter.Backend, AutoCloseable {
    private final ManagedMemoryDomain.Pressure pressure;
    private final AtomicLong rejected = new AtomicLong();
    private final AtomicInteger lastStatus = new AtomicInteger();

    public ManagedPressureBackend(ManagedMemoryDomain.Pressure pressure) {
        if (pressure == null) throw new IllegalArgumentException("pressure required");
        this.pressure = pressure;
    }

    @Override public int notifyPressure(int level) {
        int status;
        if (level < ManagedMemoryDomain.Pressure.NORMAL || level > ManagedMemoryDomain.Pressure.CRITICAL) status = 1;
        else try {
            long result = pressure.submit(level);
            status = result > 0 ? 0 : result == 0 ? 9 : (int)-result;
        } catch (ManagedMemoryDomain.ManagedException error) {
            status = error.status() > 0 ? error.status() : 9;
        } catch (RuntimeException error) {
            // Report unexpected runtime failures as an unavailable backend.
            status = 9;
        }
        lastStatus.set(status);
        if (status != 0) {
            long previous = rejected.get();
            while (previous != Long.MAX_VALUE && !rejected.compareAndSet(previous, previous + 1))
                previous = rejected.get();
        }
        return status;
    }

    /** Saturating count of rejected notifications, including framework calls
     * whose void callbacks cannot return an error to Android. */
    public long rejectedEvents() { return rejected.get(); }
    /** Most recently reported status; zero means the event was queued. */
    public int lastNotificationStatus() { return lastStatus.get(); }

    @Override public void beginClose() { pressure.beginClose(); }
    @Override public void close() { beginClose(); }
    @Override public long[] snapshot() { return pressure.snapshot(); }

    /** Positive waits are allowed only on a worker. A timeout does not release
     * native resources and callers may retry after cleanup progresses. */
    @Override public boolean awaitClosed(long timeoutMillis) {
        if (timeoutMillis < 0 || timeoutMillis > 60_000) throw new IllegalArgumentException("bounded timeout required");
        if (timeoutMillis > 0 && Looper.myLooper() == Looper.getMainLooper())
            throw new IllegalStateException("positive close waits require a worker thread");
        final long deadline = System.nanoTime() + timeoutMillis * 1_000_000L;
        do {
            int state = pressure.state();
            if (state == ManagedMemoryDomain.CLOSED) return true;
            if (state == ManagedMemoryDomain.QUARANTINED || timeoutMillis == 0) return false;
            long remaining = deadline - System.nanoTime();
            if (remaining <= 0) return false;
            try { Thread.sleep(Math.min(10L, Math.max(1L, remaining / 1_000_000L))); }
            catch (InterruptedException error) { Thread.currentThread().interrupt(); return false; }
        } while (true);
    }
}
