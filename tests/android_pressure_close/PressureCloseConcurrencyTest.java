import android.content.ComponentCallbacks2;
import android.content.Context;
import io.github.jichuo1.meguviz.memory.android.MemoryPressureAdapter;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Deterministic host-stub regression; not an Android runtime test. */
public final class PressureCloseConcurrencyTest {
    private static int checks;
    private static void check(boolean value, String message) {
        ++checks;
        if (!value) throw new AssertionError(message);
    }

    private static final class Gate {
        final CountDownLatch entered = new CountDownLatch(1);
        final CountDownLatch proceed = new CountDownLatch(1);
        void waitForRelease() {
            entered.countDown();
            try {
                if (!proceed.await(3, TimeUnit.SECONDS)) throw new AssertionError("gate timeout");
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new AssertionError(error);
            }
        }
    }

    private static final class TestContext extends Context {
        int unregisterCalls;
        Gate gate;
        boolean failOnce;
        Runnable reenter;
        @Override public void registerComponentCallbacks(ComponentCallbacks2 callback) {}
        @Override public void unregisterComponentCallbacks(ComponentCallbacks2 callback) {
            ++unregisterCalls;
            Gate current = gate;
            gate = null;
            if (current != null) current.waitForRelease();
            if (reenter != null) reenter.run();
            if (failOnce) { failOnce = false; throw new IllegalStateException("unregister failure"); }
        }
    }

    private static final class TestBackend implements MemoryPressureAdapter.Backend {
        int beginCalls;
        Gate gate;
        boolean failOnce;
        boolean accepted;
        Runnable reenter;
        @Override public int notifyPressure(int pressure) { return 0; }
        @Override public void beginClose() {
            ++beginCalls;
            Gate current = gate;
            gate = null;
            if (current != null) current.waitForRelease();
            if (reenter != null) reenter.run();
            if (failOnce) { failOnce = false; throw new IllegalStateException("cleanup admission failure"); }
            accepted = true;
        }
        @Override public boolean awaitClosed(long timeoutMillis) { return accepted; }
        @Override public long[] snapshot() { return null; }
    }

    private static void concurrentClose(boolean blockUnregister, boolean fail) throws Exception {
        TestContext context = new TestContext();
        TestBackend backend = new TestBackend();
        MemoryPressureAdapter adapter = new MemoryPressureAdapter(context, backend);
        Gate gate = new Gate();
        if (blockUnregister) { context.gate = gate; context.failOnce = fail; }
        else { backend.gate = gate; backend.failOnce = fail; }
        AtomicReference<Throwable> outerFailure = new AtomicReference<>();
        Thread outer = new Thread(() -> {
            try { adapter.close(); } catch (Throwable error) { outerFailure.set(error); }
        }, "pressure-outer-close");
        outer.setDaemon(true);
        outer.start();
        check(gate.entered.await(2, TimeUnit.SECONDS), "first close did not enter gate");
        boolean rejected = false;
        try {
            check(adapter.isClosed(), "events should be rejected during close");
            check(adapter.requestPressure(MemoryPressureAdapter.NORMAL) == 7, "closed admission");
            try { adapter.close(); } catch (IllegalStateException expected) { rejected = true; }
        } finally { gate.proceed.countDown(); }
        outer.join(3000);
        check(!outer.isAlive(), "outer close must finish");
        check(rejected, "a concurrent close returned normally before admission completed");
        check((outerFailure.get() != null) == fail, "outer result must be visible to its caller");
        if (fail) {
            check(outerFailure.get() instanceof IllegalStateException, "expected injected failure");
            adapter.close();
        }
        adapter.close();
        check(context.unregisterCalls == (blockUnregister && fail ? 2 : 1), "unregister retry count");
        check(backend.beginCalls == (!blockUnregister && fail ? 2 : 1), "cleanup admission retry count");
        check(backend.accepted && adapter.awaitClosed(0), "cleanup should be accepted after retry");
    }

    private static void sameThreadReentry() throws Exception {
        TestContext context = new TestContext();
        TestBackend backend = new TestBackend();
        MemoryPressureAdapter adapter = new MemoryPressureAdapter(context, backend);
        context.reenter = adapter::close;
        backend.reenter = adapter::close;
        AtomicReference<Throwable> failure = new AtomicReference<>();
        Thread closer = new Thread(() -> {
            try { adapter.close(); } catch (Throwable error) { failure.set(error); }
        }, "pressure-reentrant-close");
        closer.setDaemon(true);
        closer.start();
        closer.join(3000);
        check(!closer.isAlive(), "same-thread reentry deadlocked");
        check(failure.get() == null, "same-thread reentry failed");
        check(context.unregisterCalls == 1 && backend.beginCalls == 1 && backend.accepted,
                "same-thread reentry duplicated close actions");
    }

    public static void main(String[] args) throws Exception {
        concurrentClose(true, true);
        concurrentClose(false, true);
        concurrentClose(true, false);
        concurrentClose(false, false);
        sameThreadReentry();
        System.out.println("PASS pressure close concurrency " + checks + " checks (host stubs)");
    }
}
