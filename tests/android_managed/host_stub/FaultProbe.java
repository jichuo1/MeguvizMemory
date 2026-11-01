import android.content.ComponentCallbacks2;
import android.content.Context;
import io.github.jichuo1.meguviz.memory.android.MemoryPressureAdapter;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Deterministic host-only framework and backend faults. No native domain is involved. */
public final class FaultProbe {
    private static final class ContextStub extends Context {
        int registered, unregistered; boolean failRegister, failUnregister;
        @Override public void registerComponentCallbacks(ComponentCallbacks2 callback) {
            if (failRegister) throw new IllegalStateException(); ++registered;
        }
        @Override public void unregisterComponentCallbacks(ComponentCallbacks2 callback) {
            if (failUnregister) { failUnregister = false; throw new IllegalStateException(); } ++unregistered;
        }
    }
    private static final class Backend implements MemoryPressureAdapter.Backend {
        int notifications, closes; boolean failClose;
        CountDownLatch entered, proceed;
        public int notifyPressure(int level) { ++notifications; return 6; }
        public void beginClose() {
            ++closes;
            if (entered != null) {
                entered.countDown();
                try { if (!proceed.await(3, TimeUnit.SECONDS)) throw new AssertionError(); }
                catch (InterruptedException error) { Thread.currentThread().interrupt(); throw new AssertionError(); }
            }
            if (failClose) { failClose = false; throw new IllegalStateException(); }
        }
        public boolean awaitClosed(long timeout) { return false; } // no completion confirmation
        public long[] snapshot() { return null; }
    }
    private static void check(boolean value) { if (!value) throw new AssertionError(); }
    private static void run() throws Exception {
        ContextStub fail = new ContextStub(); Backend abandoned = new Backend(); fail.failRegister = true;
        try { new MemoryPressureAdapter(fail, abandoned); throw new AssertionError(); }
        catch (IllegalStateException expected) { check(abandoned.closes == 1 && fail.registered == 0); }
        ContextStub c = new ContextStub(); Backend b = new Backend();
        MemoryPressureAdapter a = new MemoryPressureAdapter(c, b);
        a.onTrimMemory(15); check(b.notifications == 1 && a.forwardedEvents() == 0);
        check(!a.awaitClosed(0)); // no queued/cleanup admission is misreported as completion
        c.failUnregister = true; b.failClose = true;
        try { a.close(); throw new AssertionError(); } catch (IllegalStateException expected) { check(a.isClosed()); }
        check(a.requestPressure(0) == 7);
        a.close(); a.close(); check(c.registered == 1 && c.unregistered == 1 && b.closes == 2);
        ContextStub c2 = new ContextStub(); Backend b2 = new Backend();
        b2.entered = new CountDownLatch(1); b2.proceed = new CountDownLatch(1);
        MemoryPressureAdapter a2 = new MemoryPressureAdapter(c2, b2);
        AtomicReference<Throwable> error = new AtomicReference<>();
        Thread first = new Thread(() -> { try { a2.close(); } catch (Throwable t) { error.set(t); } });
        first.start();
        try {
            check(b2.entered.await(3, TimeUnit.SECONDS));
            try { a2.close(); throw new AssertionError(); } catch (IllegalStateException expected) { check(a2.isClosed()); }
        } finally { b2.proceed.countDown(); }
        first.join(4000); check(!first.isAlive() && error.get() == null);
        a2.close(); check(c2.unregistered == 1 && b2.closes == 1 && !a2.awaitClosed(0));
    }
    public static void main(String[] args) {
        try { run(); System.out.println("{\"suite\":\"android-managed-fault-host\",\"status\":\"PASS\"}"); }
        catch (Throwable error) { System.out.println("{\"suite\":\"android-managed-fault-host\",\"status\":\"FAIL\"}"); System.exit(1); }
    }
}
