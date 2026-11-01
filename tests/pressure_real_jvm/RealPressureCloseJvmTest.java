import android.content.ComponentCallbacks2;
import android.content.Context;
import io.github.jichuo1.meguviz.memory.android.MemoryPressureAdapter;
import io.github.jichuo1.meguviz.memory.android.NativePressureBackend;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/** Real JVM and native composition; Android framework types are host stubs. */
public final class RealPressureCloseJvmTest {
    private static int checks;
    private static void check(boolean value, String message) {
        ++checks;
        if (!value) throw new AssertionError(message);
    }
    private static final class TestContext extends Context {
        final AtomicInteger registrations = new AtomicInteger(), unregistrations = new AtomicInteger();
        @Override public void registerComponentCallbacks(ComponentCallbacks2 callback) { registrations.incrementAndGet(); }
        @Override public void unregisterComponentCallbacks(ComponentCallbacks2 callback) { unregistrations.incrementAndGet(); }
    }
    private static long[] snapshot(NativePressureBackend backend) throws Exception {
        long deadline = System.nanoTime() + 3_000_000_000L;
        long[] values;
        while ((values = backend.snapshot()) == null && System.nanoTime() < deadline) Thread.sleep(2);
        if (values == null) throw new AssertionError("snapshot unavailable");
        return values;
    }
    private static void attempt(Runnable action, CountDownLatch start, AtomicReference<Throwable> failure) {
        try {
            if (!start.await(3, TimeUnit.SECONDS)) throw new AssertionError("start gate timeout");
            action.run();
        } catch (Throwable error) { failure.set(error); }
    }
    private static boolean acceptedOrContended(Throwable error) {
        return error == null || (error instanceof IllegalStateException &&
            error.getMessage() != null && error.getMessage().contains("in progress; retry"));
    }
    private static void cycle() throws Exception {
        TestContext context = new TestContext();
        NativePressureBackend backend = NativePressureBackend.createArena(4096, 2048, 0, 256);
        MemoryPressureAdapter adapter = new MemoryPressureAdapter(context, backend);
        NativePressureBackend.LeaseProbe probe = backend.holdRetiringLeaseForDiagnostics(1024);
        check(context.registrations.get() == 1 && probe.readable(), "initial registration and lease");
        long[] initial = snapshot(backend);
        check(initial.length == 16 && initial[3] == 1024 && initial[6] == 1024, "retiring backing charged once");
        adapter.onTrimMemory(15); // Direct invocation is synthetic, not a framework delivery claim.
        long deadline = System.nanoTime() + 3_000_000_000L;
        long[] pressured;
        do { pressured = snapshot(backend); if (pressured[2] == 0) break; Thread.sleep(2); }
        while (System.nanoTime() < deadline);
        check(pressured[2] == 0 && pressured[3] == 1024 && pressured[6] == 1024 && probe.readable(),
              "pressure preserves the held allocation");

        CountDownLatch start = new CountDownLatch(1);
        AtomicReference<Throwable> direct = new AtomicReference<>(), viaAdapter = new AtomicReference<>();
        Thread a = new Thread(() -> attempt(backend::beginClose, start, direct), "real-jni-direct-close");
        Thread b = new Thread(() -> attempt(adapter::close, start, viaAdapter), "real-jni-adapter-close");
        a.setDaemon(true); b.setDaemon(true); a.start(); b.start(); start.countDown();
        a.join(5000); b.join(5000);
        check(!a.isAlive() && !b.isAlive(), "close callers complete");
        check(acceptedOrContended(direct.get()) && acceptedOrContended(viaAdapter.get()), "close outcome");
        // Once both admission callers returned, retry an explicitly rejected
        // in-flight call; the held probe prevents final domain closure.
        backend.beginClose(); adapter.close();
        check(context.unregistrations.get() == 1 && adapter.isClosed(), "unregister once");
        check(adapter.requestPressure(MemoryPressureAdapter.NORMAL) == 7, "closed callback rejected");
        check(probe.readable() && !adapter.awaitClosed(15), "held lease survives close and timeout");
        probe.close(); probe.close();
        check(adapter.awaitClosed(5000), "real JNI completion callback");
        long[] result = snapshot(backend);
        check(result.length == 16 && result[0] == 2 && result[1] == 2 && result[3] == 0 &&
              result[4] == 0 && result[5] == 0 && result[6] == 0 && result[7] == 0 &&
              result[13] == 0 && result[14] == 1 && result[15] == 1, "final closed snapshot and zero accounting");
        adapter.close(); backend.beginClose();
        check(adapter.awaitClosed(0), "repeated close after completion");
    }
    public static void main(String[] args) throws Exception {
        for (int round = 0; round < 24; ++round) cycle();
        System.out.println("PASS real host JVM/JNI pressure close: 24 cycles, " + checks + " checks");
    }
}
