package io.github.jichuo1.meguviz.memory.android;

import android.app.Instrumentation;
import android.os.Bundle;
import android.os.Looper;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.json.JSONObject;

/** Independent short functional probe. All pressure callbacks are synthetic. */
public final class NativePressureCloseInstrumentation extends Instrumentation {
    private static final int ROUNDS = 4;
    private int checks;
    private String stage = "start";
    private boolean resourcesPossible;

    private static final class Incomplete extends Exception {}
    private void check(boolean condition) { ++checks; if (!condition) throw new AssertionError(stage); }
    @Override public void onCreate(Bundle args) { super.onCreate(args); start(); }

    private long[] snapshot(NativePressureBackend backend) throws Exception {
        long deadline = System.nanoTime() + 3_000_000_000L;
        long[] values;
        while ((values = backend.snapshot()) == null && System.nanoTime() < deadline) Thread.sleep(2);
        if (values == null) throw new Incomplete();
        return values;
    }
    private static boolean acceptedOrContended(Throwable error) {
        return error == null || (error instanceof IllegalStateException &&
            "cleanup admission in progress; retry".equals(error.getMessage()));
    }
    private static void attempt(Runnable action, CountDownLatch ready, CountDownLatch go,
                                AtomicReference<Throwable> failure) {
        ready.countDown();
        try {
            if (!go.await(3, TimeUnit.SECONDS)) throw new Incomplete();
            action.run();
        } catch (Throwable error) { failure.set(error); }
    }

    private void cycle() throws Exception {
        stage = "create";
        // A failed construction may have unknown native state; do not force cleanup.
        resourcesPossible = true;
        NativePressureBackend backend = NativePressureBackend.createArena(4096, 2048, 0, 256);
        MemoryPressureAdapter adapter = new MemoryPressureAdapter(getTargetContext(), backend);
        NativePressureBackend.LeaseProbe probe = backend.holdRetiringLeaseForDiagnostics(1024);
        check(probe.readable());
        long[] initial = snapshot(backend);
        check(initial.length == 16 && initial[3] == 1024 && initial[6] == 1024);

        stage = "synthetic_pressure";
        long deadline = System.nanoTime() + 3_000_000_000L;
        do {
            adapter.onTrimMemory(15); // Direct call, not a real Android low-memory event.
            if (adapter.forwardedEvents() != 0) break;
            Thread.sleep(2);
        } while (System.nanoTime() < deadline);
        if (adapter.forwardedEvents() == 0) throw new Incomplete();
        check(adapter.forwardedEvents() == 1);
        deadline = System.nanoTime() + 3_000_000_000L;
        long[] pressured;
        do { pressured = snapshot(backend); if (pressured[2] == 0) break; Thread.sleep(2); }
        while (System.nanoTime() < deadline);
        if (pressured[2] != 0) throw new Incomplete();
        check(pressured[2] == 0 && pressured[3] == 1024 && pressured[6] == 1024 && probe.readable());

        stage = "concurrent_close";
        CountDownLatch ready = new CountDownLatch(2), go = new CountDownLatch(1);
        AtomicReference<Throwable> direct = new AtomicReference<>(), viaAdapter = new AtomicReference<>();
        Thread a = new Thread(() -> attempt(backend::beginClose, ready, go, direct), "native-probe-backend-close");
        Thread b = new Thread(() -> attempt(adapter::close, ready, go, viaAdapter), "native-probe-adapter-close");
        a.setDaemon(true); b.setDaemon(true); a.start(); b.start();
        boolean bothReady;
        try { bothReady = ready.await(3, TimeUnit.SECONDS); }
        finally { go.countDown(); }
        a.join(5000); b.join(5000);
        // An in-flight JNI call cannot be cancelled or safely treated as closed.
        if (!bothReady || a.isAlive() || b.isAlive()) throw new Incomplete();
        boolean outcomesKnown = acceptedOrContended(direct.get()) && acceptedOrContended(viaAdapter.get());
        if (!outcomesKnown) throw new Incomplete();
        check(outcomesKnown);
        // Retry only after both callers returned and only on the expected contention.
        backend.beginClose(); adapter.close();
        check(adapter.isClosed());
        check(adapter.requestPressure(MemoryPressureAdapter.NORMAL) == 7);
        check(probe.readable());
        boolean closedDuringLease = adapter.awaitClosed(15);
        if (Thread.currentThread().isInterrupted()) throw new Incomplete();
        check(!closedDuringLease);
        check(probe.readable());

        stage = "final_close";
        probe.close(); probe.close();
        if (!adapter.awaitClosed(5000)) throw new Incomplete();
        check(adapter.awaitClosed(0));
        long[] last = snapshot(backend);
        check(last.length == 16 && last[0] == 2 && last[1] == 2 && last[3] == 0 &&
            last[4] == 0 && last[5] == 0 && last[6] == 0 && last[7] == 0 &&
            last[13] == 0 && last[14] == 1 && last[15] == 1);
        adapter.close(); backend.beginClose();
        check(adapter.awaitClosed(0));
        resourcesPossible = false;
    }

    @Override public void onStart() {
        Bundle result = new Bundle();
        try {
            check(Looper.myLooper() != Looper.getMainLooper());
            for (int round = 0; round < ROUNDS; ++round) cycle();
            JSONObject report = new JSONObject();
            report.put("suite", "android-native-pressure-close-v1");
            report.put("status", "PASS"); report.put("checks", checks);
            report.put("case_count", ROUNDS); report.put("final_backing_bytes", 0);
            report.put("callback_origin", "synthetic");
            report.put("race_window_guaranteed", false); report.put("performance_qualified", false);
            result.putString("report_json", report.toString()); finish(0, result);
        } catch (Throwable error) {
            // A fixed stage is sufficient; never disclose exception text or addresses.
            String status = error instanceof AssertionError ? "FAIL" : "INCOMPLETE";
            result.putString("report_json", "{\"suite\":\"android-native-pressure-close-v1\",\"status\":\"" + status +
                "\",\"checks\":" + checks + ",\"stage\":\"" + stage + "\"}");
            if (!resourcesPossible) finish(1, result);
            else sendStatus(1, result); // Unknown/in-flight resources are not forcibly released.
        }
    }
}
