package io.github.jichuo1.meguviz.memory.android;

import android.app.Instrumentation;
import android.os.Bundle;
import android.os.Looper;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.json.JSONObject;

/** Isolated diagnostic. Framework callback calls below are synthetic. */
public final class ManagedInstrumentation extends Instrumentation {
    private int checks;
    private String stage = "start";
    private void check(boolean ok) { ++checks; if (!ok) throw new AssertionError(stage); }
    @Override public void onCreate(Bundle args) { super.onCreate(args); start(); }

    private long[] apply(ManagedMemoryDomain.Pressure p, int level) throws Exception {
        long deadline = System.nanoTime() + 5_000_000_000L, ticket = -6;
        while (ticket == -6 && System.nanoTime() < deadline) {
            ticket = p.submit(level);
            if (ticket == -6) Thread.sleep(2);
        }
        check(ticket > 0); // Admission is not application.
        long[] result;
        do {
            result = p.eventResult(ticket);
            check(result != null && result.length == 4 && result[2] == ticket);
            if (result[0] != ManagedMemoryDomain.Pressure.EVENT_QUEUED) break;
            Thread.sleep(2);
        } while (System.nanoTime() < deadline);
        check(result[0] == ManagedMemoryDomain.Pressure.EVENT_APPLIED && result[1] == 0 && result[3] > 0);
        return result;
    }
    private void waitPressure(ManagedMemoryDomain.Pressure p) throws Exception {
        long deadline = System.nanoTime() + 8_000_000_000L;
        while (p.state() == ManagedMemoryDomain.CLOSING && System.nanoTime() < deadline) Thread.sleep(2);
        check(p.state() == ManagedMemoryDomain.CLOSED);
    }
    private void waitDomain(ManagedMemoryDomain d) throws Exception {
        long deadline = System.nanoTime() + 8_000_000_000L;
        while (d.state() == ManagedMemoryDomain.CLOSING && System.nanoTime() < deadline) Thread.sleep(2);
        check(d.state() == ManagedMemoryDomain.CLOSED);
    }
    private void runDomain(boolean arena) throws Exception {
        stage = arena ? "arena_create" : "system_create";
        ManagedMemoryDomain d = arena ? ManagedMemoryDomain.arena(8192, 4096) : ManagedMemoryDomain.system(8192);
        ManagedMemoryDomain.Allocation owner = d.allocate(128, 8);
        ManagedMemoryDomain.Lease lease = owner.borrowWrite(0, 128);
        byte[] bytes = new byte[128];
        lease.read(0, bytes, 0, bytes.length);
        for (byte b : bytes) check(b == 0);
        byte[] value = {7, 11, 13};
        lease.write(4, value, 0, value.length);
        ManagedMemoryDomain.Pressure p = d.attachPressure(4096, 1024);
        stage = arena ? "arena_budget" : "system_budget";
        apply(p, ManagedMemoryDomain.Pressure.CRITICAL);
        long[] ps = p.snapshot(), ds = d.snapshot();
        check(ps != null && ps.length >= 20 && ps[6] == 8192 && ps[7] == 1024);
        check(ds != null && ds.length == 10 && ds[2] == 1024 && ds[3] > 0);
        // Arena already owns a chunk; zero manual budget yields deterministic rejection for both kinds.
        d.setBudget(0);
        apply(p, ManagedMemoryDomain.Pressure.NORMAL);
        ps = p.snapshot(); ds = d.snapshot();
        check(ps != null && ps[6] == 0 && ps[7] == 0 && ds != null && ds[2] == 0);
        try { d.allocate(1, 8); throw new AssertionError(stage); }
        catch (ManagedMemoryDomain.ManagedException expected) { check(expected.status() == 4); }
        stage = arena ? "arena_close" : "system_close";
        owner.close();
        d.beginClose(); d.beginClose();
        waitPressure(p);
        check(d.state() == ManagedMemoryDomain.CLOSING);
        ds = d.snapshot();
        check(ds != null && ds[3] > 0 && ds[7] == 128 && ds[8] == 1 && ds[9] == 1);
        byte[] actual = new byte[3]; lease.read(4, actual, 0, 3);
        check(actual[0] == 7 && actual[1] == 11 && actual[2] == 13);
        lease.write(4, new byte[]{19}, 0, 1); lease.read(4, actual, 0, 1); check(actual[0] == 19);
        lease.close(); lease.close();
        waitDomain(d);
        ds = d.snapshot();
        check(ds != null && ds[3] == 0 && ds[4] == 0 && ds[6] == 0 && ds[7] == 0 && ds[8] == 0 && ds[9] == 0);
        d.forgetClosed();
    }
    private void callbacks() throws Exception {
        stage = "callback_setup";
        ManagedMemoryDomain d = ManagedMemoryDomain.system(8192);
        ManagedMemoryDomain.Pressure p = d.attachPressure(4096, 1024);
        ManagedPressureBackend backend = new ManagedPressureBackend(p);
        MemoryPressureAdapter adapter = new MemoryPressureAdapter(getTargetContext(), backend);
        adapter.onTrimMemory(20); // UI hidden is ignored, not pressure.
        long deadline = System.nanoTime() + 5_000_000_000L;
        int attempts = 0, status = 6;
        while (attempts < 100 && status == 6 && System.nanoTime() < deadline) {
            adapter.onTrimMemory(15); // Synthetic callback, not a system low-memory event.
            ++attempts;
            status = backend.lastNotificationStatus();
            check((status == 6 && backend.rejectedEvents() == attempts && adapter.forwardedEvents() == 0) ||
                (status == 0 && backend.rejectedEvents() == attempts - 1 && adapter.forwardedEvents() == 1));
            if (status == 6) Thread.sleep(2);
        }
        check(attempts > 0 && status == 0 && adapter.ignoredLifecycleEvents() == 1 && adapter.forwardedEvents() == 1);
        // A distinct ticket is confirmed applied; the void callback carries no ticket.
        apply(p, ManagedMemoryDomain.Pressure.CRITICAL);
        check(p.snapshot()[7] == 1024);
        stage = "callback_concurrent_close";
        CountDownLatch start = new CountDownLatch(1);
        AtomicReference<Throwable> problem = new AtomicReference<>();
        Runnable close = () -> {
            try { if (!start.await(3, TimeUnit.SECONDS)) throw new AssertionError(); adapter.close(); }
            catch (IllegalStateException retryable) { /* concurrent outer close may still be in progress */ }
            catch (Throwable error) { problem.compareAndSet(null, error); }
        };
        Thread a = new Thread(close, "managed-adapter-close-a"), b = new Thread(close, "managed-adapter-close-b");
        a.start(); b.start(); start.countDown(); a.join(5000); b.join(5000);
        check(!a.isAlive() && !b.isAlive() && problem.get() == null);
        adapter.close(); adapter.close();
        check(adapter.isClosed() && adapter.requestPressure(MemoryPressureAdapter.NORMAL) == 7);
        check(adapter.awaitClosed(8000));
        d.beginClose(); waitDomain(d);
        long[] ds = d.snapshot();
        check(ds != null && ds[3] == 0 && ds[4] == 0 && ds[8] == 0 && ds[9] == 0);
        d.forgetClosed();
    }
    @Override public void onStart() {
        Bundle result = new Bundle();
        try {
            check(Looper.myLooper() != Looper.getMainLooper());
            runDomain(false); runDomain(true); callbacks();
            JSONObject report = new JSONObject();
            report.put("suite", "android-managed-pressure-v1"); report.put("status", "PASS");
            report.put("checks", checks); report.put("case_count", 3);
            report.put("final_backing_bytes", 0); report.put("callback_origin", "synthetic");
            report.put("queued_implies_applied", false); report.put("performance_qualified", false);
            result.putString("report_json", report.toString()); finish(0, result);
        } catch (Throwable error) {
            // stage is a fixed literal; never include exception messages, addresses or payload data.
            result.putString("report_json", "{\"suite\":\"android-managed-pressure-v1\",\"status\":\"FAIL\",\"checks\":" + checks + ",\"stage\":\"" + stage + "\"}");
            finish(1, result);
        }
    }
}
