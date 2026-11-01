package io.github.jichuo1.meguviz.memory.android;

import android.content.ComponentCallbacks2;
import android.content.Context;
import android.content.res.Configuration;
import android.os.Looper;
import java.util.concurrent.atomic.AtomicLong;

/** Framework callback registration. Notifications must not perform cleanup or
 * wait for it; short admission locks may still contend and have no hard timing
 * bound. Observation and awaitClosed belong on a caller-selected worker. No UI
 * buffer, Bitmap, HWUI or another library's resource is reclaimed by this adapter. */
public final class MemoryPressureAdapter implements ComponentCallbacks2, AutoCloseable {
    public static final int NORMAL = 0, MODERATE = 1, CRITICAL = 2;
    public interface Backend {
        int notifyPressure(int pressure);
        void beginClose();
        boolean awaitClosed(long timeoutMillis);
        long[] snapshot();
    }
    private final Context context;
    private final Backend backend;
    private final Object state = new Object();
    private boolean closed, unregistered, cleanupAccepted, closeInProgress;
    private final AtomicLong forwarded = new AtomicLong(), ignored = new AtomicLong();

    public MemoryPressureAdapter(Context context, Backend backend) {
        if (context == null || backend == null) throw new IllegalArgumentException("context/backend required");
        Context application = context.getApplicationContext();
        this.context = application == null ? context : application;
        this.backend = backend;
        try { this.context.registerComponentCallbacks(this); }
        catch (RuntimeException | Error error) {
            closed = true;
            try { backend.beginClose(); } catch (RuntimeException cleanup) { error.addSuppressed(cleanup); }
            throw error;
        }
    }
    /** UI_HIDDEN is lifecycle information, not proof of memory pressure.
     * Restore is explicit; newer intermediate pressure levels use ranges. */
    public static int classify(int level) {
        if (level >= 80) return CRITICAL;
        if (level >= 40) return MODERATE;
        if (level >= 20) return -1;
        if (level >= 15) return CRITICAL;
        if (level >= 5) return MODERATE;
        return -1;
    }
    public int requestPressure(int pressure) {
        if (pressure < NORMAL || pressure > CRITICAL) throw new IllegalArgumentException("unknown pressure");
        synchronized (state) {
            if (closed) return 7; // MGMEM_IS_CLOSED
        }
        // No caller-controlled backend operation runs under state. A native
        // backend pins its registration for the complete call; racing close
        // either follows an accepted event or returns IS_CLOSED.
        int status = backend.notifyPressure(pressure);
        if (status == 0) forwarded.incrementAndGet();
        return status;
    }
    public int restoreBudget() { return requestPressure(NORMAL); }
    @Override public void onTrimMemory(int level) {
        int pressure = classify(level);
        if (pressure < 0) { ignored.incrementAndGet(); return; }
        requestPressure(pressure);
    }
    @Override public void onLowMemory() { requestPressure(CRITICAL); }
    @Override public void onConfigurationChanged(Configuration configuration) {}
    public long forwardedEvents() { return forwarded.get(); }
    public long ignoredLifecycleEvents() { return ignored.get(); }
    public boolean isClosed() { synchronized (state) { return closed; } }
    public long[] logicalSnapshot() { return backend.snapshot(); }
    public boolean awaitClosed(long timeoutMillis) {
        if (timeoutMillis < 0 || timeoutMillis > 60_000) throw new IllegalArgumentException("bounded timeout required");
        if (timeoutMillis>0 && Looper.myLooper()==Looper.getMainLooper())
            throw new IllegalStateException("positive close waits require a worker thread");
        return backend.awaitClosed(timeoutMillis);
    }
    @Override public void close() {
        final boolean unregister, cleanup;
        synchronized (state) {
            closed = true;
            if (closeInProgress || (unregistered && cleanupAccepted)) return;
            closeInProgress = true;
            unregister = !unregistered; cleanup = !cleanupAccepted;
        }
        // Unregister outside state: framework callback completion/close may
        // reenter. Failed setup/cleanup admission remains explicitly retryable;
        // callbacks stay rejected and successful steps never run twice.
        try {
            try {
                if (unregister) {
                    context.unregisterComponentCallbacks(this);
                    synchronized (state) { unregistered = true; }
                }
            } finally {
                if (cleanup) {
                    backend.beginClose();
                    synchronized (state) { cleanupAccepted = true; }
                }
            }
        } finally { synchronized (state) { closeInProgress = false; } }
    }
}
