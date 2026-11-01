import android.content.ComponentCallbacks2;
import android.content.Context;
import io.github.jichuo1.meguviz.memory.android.MemoryPressureAdapter;
import io.github.jichuo1.meguviz.memory.android.NativePressureBackend;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/** Fake-JNI contract test; never loads the production JNI or an Android runtime. */
public final class NativeBackendConcurrencyTest {
    private static final AtomicInteger calls = new AtomicInteger();
    private static volatile int mode;
    private static volatile CountDownLatch entered, proceed;
    private static volatile NativePressureBackend reentrant;
    private static volatile boolean reentryRejected;
    private static int checks;

    private static void check(boolean value, String message) {
        ++checks;
        if (!value) throw new AssertionError(message);
    }
    private static final class TestContext extends Context {
        int unregistrations;
        @Override public void registerComponentCallbacks(ComponentCallbacks2 callback) {}
        @Override public void unregisterComponentCallbacks(ComponentCallbacks2 callback) { ++unregistrations; }
    }

    // Called by the fake native method in the initiating Java thread.
    public static int onNativeBeginClose() throws Exception {
        calls.incrementAndGet();
        int current = mode;
        if (current == 1 || current == 2) {
            entered.countDown();
            if (!proceed.await(5, TimeUnit.SECONDS)) throw new AssertionError("native gate timeout");
            mode = 0;
            return current == 1 ? 3 : 0;
        }
        if (current == 3) throw new IllegalStateException("fake JNI outcome unknown");
        if (current == 4) {
            try { reentrant.beginClose(); }
            catch (IllegalStateException expected) { reentryRejected = true; }
        }
        return 0;
    }
    private static NativePressureBackend newBackend() {
        return NativePressureBackend.createArena(8192, 4096, 1024, 4096);
    }
    private static void concurrentAdapterClose(boolean firstFails) throws Exception {
        calls.set(0); mode = firstFails ? 1 : 2;
        entered = new CountDownLatch(1); proceed = new CountDownLatch(1);
        NativePressureBackend backend = newBackend();
        TestContext context = new TestContext();
        MemoryPressureAdapter adapter = new MemoryPressureAdapter(context, backend);
        AtomicReference<Throwable> firstError = new AtomicReference<>();
        Thread first = new Thread(() -> {
            try { backend.beginClose(); } catch (Throwable error) { firstError.set(error); }
        });
        first.setDaemon(true); first.start();
        check(entered.await(3, TimeUnit.SECONDS), "first admission did not enter fake JNI");
        boolean rejected = false;
        try { adapter.close(); } catch (IllegalStateException expected) { rejected = true; }
        finally { proceed.countDown(); }
        first.join(3000);
        check(!first.isAlive(), "first admission did not finish");
        check(rejected, "adapter falsely accepted an in-progress backend close");
        check((firstError.get() != null) == firstFails, "first admission outcome");
        adapter.close();
        adapter.close();
        check(context.unregistrations == 1, "unregister repeated after retry");
        check(calls.get() == (firstFails ? 2 : 1), "native close retry/idempotence");
        check(backend.notifyPressure(0) == 7, "accepted close rejects events");
    }
    private static void unknownOutcome() {
        calls.set(0); mode = 3;
        NativePressureBackend backend = newBackend();
        boolean failed = false;
        try { backend.beginClose(); } catch (IllegalStateException expected) { failed = true; }
        check(failed, "JNI exception did not propagate");
        mode = 0;
        boolean unknown = false;
        try { backend.beginClose(); } catch (IllegalStateException expected) { unknown = true; }
        check(unknown && calls.get() == 1, "unknown result was retried or reported accepted");
        check(backend.notifyPressure(0) == 7, "unknown close must not reopen admission");
        check(!backend.awaitClosed(0), "unknown result is not completed close");
        signalCompleted(backend, 0);
        check(backend.awaitClosed(0), "independent completion callback should resolve unknown outcome");
        backend.beginClose();
        check(calls.get() == 1, "confirmed completion must not resubmit close");
    }
    private static void signalCompleted(NativePressureBackend backend, int status) {
        try {
            java.lang.reflect.Method callback = NativePressureBackend.class.getDeclaredMethod("onNativeClosed", int.class);
            callback.setAccessible(true);
            callback.invoke(backend, status);
        } catch (ReflectiveOperationException error) { throw new AssertionError(error); }
    }
    private static void reentry() {
        calls.set(0); mode = 4; reentryRejected = false;
        reentrant = newBackend();
        reentrant.beginClose();
        check(reentryRejected, "same-thread in-flight close falsely returned success");
        reentrant.beginClose();
        check(calls.get() == 1, "accepted close must be idempotent");
    }
    private static void completedWhileAdmissionReturnsFailure() throws Exception {
        calls.set(0); mode = 1;
        entered = new CountDownLatch(1); proceed = new CountDownLatch(1);
        NativePressureBackend backend = newBackend();
        AtomicReference<Throwable> error = new AtomicReference<>();
        Thread first = new Thread(() -> {
            try { backend.beginClose(); } catch (Throwable failure) { error.set(failure); }
        });
        first.setDaemon(true); first.start();
        check(entered.await(3, TimeUnit.SECONDS), "admission did not enter fake JNI");
        signalCompleted(backend, 0); // An independent native unregister completed.
        proceed.countDown(); first.join(3000);
        check(!first.isAlive() && error.get() instanceof IllegalStateException, "first error must be visible");
        check(backend.awaitClosed(0), "confirmed completion was lost");
        backend.beginClose();
        check(calls.get() == 1 && backend.notifyPressure(0) == 7, "late failure reopened confirmed close");
    }
    public static void main(String[] args) throws Exception {
        concurrentAdapterClose(true);
        concurrentAdapterClose(false);
        unknownOutcome();
        reentry();
        completedWhileAdmissionReturnsFailure();
        System.out.println("PASS native pressure backend close " + checks + " checks (fake JNI)");
    }
}
