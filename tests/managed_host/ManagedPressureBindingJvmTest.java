package io.github.jichuo1.meguviz.memory.android;

import java.lang.reflect.Method;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicReference;

/** Host-only lifecycle and budget tests for the optional same-domain binding. */
public final class ManagedPressureBindingJvmTest {
    private static native int nativePressureGate(int action);
    private static void check(boolean value, String message) {
        if (!value) throw new AssertionError(message);
    }
    private static Object hook(String name, int arg) throws Exception {
        Method method = ManagedMemoryDomainJvmTest.class.getDeclaredMethod(name, int.class);
        method.setAccessible(true);
        return method.invoke(null, arg);
    }
    private static void arm(int gate) throws Exception { hook("nativeArmGate", gate); }
    private static void entered(int gate) throws Exception { check((Boolean)hook("nativeWaitGate", gate), "gate not entered"); }
    private static void release(int gate) throws Exception { check((Boolean)hook("nativeReleaseGate", gate), "gate timed out"); }
    private static void waitDomain(ManagedMemoryDomain d) throws Exception {
        long deadline = System.nanoTime() + 8_000_000_000L;
        while (d.state() != ManagedMemoryDomain.CLOSED && System.nanoTime() < deadline) Thread.sleep(2);
        check(d.state() == ManagedMemoryDomain.CLOSED, "domain close timeout/state=" + d.state());
    }
    private static void waitPressure(ManagedMemoryDomain.Pressure p) throws Exception {
        long deadline = System.nanoTime() + 8_000_000_000L;
        while (p.state() != ManagedMemoryDomain.CLOSED && System.nanoTime() < deadline) Thread.sleep(2);
        check(p.state() == ManagedMemoryDomain.CLOSED, "pressure close timeout/state=" + p.state());
    }
    private static long[] event(ManagedMemoryDomain.Pressure p, int level) throws Exception {
        long ticket = 0;
        for (int i = 0; i < 1000 && ticket <= 0; i++) {
            ticket = p.submit(level);
            if (ticket == -6) { Thread.sleep(1); continue; }
            check(ticket > 0, "submit failed: " + ticket);
        }
        check(ticket > 0, "queue remained busy");
        long deadline = System.nanoTime() + 5_000_000_000L;
        long[] value;
        do {
            value = p.eventResult(ticket);
            check(value != null && value[2] == ticket, "ticket observation unavailable");
            if (value[0] != ManagedMemoryDomain.Pressure.EVENT_QUEUED) break;
            Thread.sleep(1);
        } while (System.nanoTime() < deadline);
        check(value[0] == ManagedMemoryDomain.Pressure.EVENT_APPLIED && value[1] == 0 && value[3] > 0,
            "event not applied");
        return value;
    }
    private static void rejectedBudget(ManagedMemoryDomain d) {
        try { d.allocate(1, 8); throw new AssertionError("zero budget admitted allocation"); }
        catch (ManagedMemoryDomain.ManagedException e) { check(e.status() == 4, "unexpected allocation status"); }
    }
    private static void zeroAndRebind() throws Exception {
        long[] before = ManagedMemoryDomain.runtimeCounts();
        ManagedMemoryDomain d = ManagedMemoryDomain.arena(8192, 4096);
        d.setBudget(0);
        ManagedMemoryDomain.Pressure p = d.attachPressure(4096, 1024);
        event(p, ManagedMemoryDomain.Pressure.NORMAL);
        long[] snap = p.snapshot();
        check(snap != null && snap.length >= 20 && snap[6] == 0 && snap[7] == 0, "zero attach raised budget");
        rejectedBudget(d);
        d.setBudget(8192);
        event(p, ManagedMemoryDomain.Pressure.CRITICAL);
        check(p.snapshot()[7] == 1024, "critical budget incorrect");
        d.setBudget(0);
        long[] last = event(p, ManagedMemoryDomain.Pressure.NORMAL);
        check(p.snapshot()[6] == 0 && p.snapshot()[7] == 0, "restore raised manual zero");
        rejectedBudget(d);
        hook("nativeInject", 1);
        try { p.beginClose(); throw new AssertionError("cleanup failure not reported"); }
        catch (ManagedMemoryDomain.ManagedException expected) { check(expected.status() == 3, "cleanup retry status"); }
        p.beginClose(); waitPressure(p);
        check(p.eventResult(last[2])[0] == ManagedMemoryDomain.Pressure.EVENT_APPLIED, "closed event unavailable");
        ManagedMemoryDomain.Pressure next = d.attachPressure(4096, 1024);
        check(p.eventResult(last[2]) == null, "old generation accessed new results");
        p.beginClose();
        check(next.state() == ManagedMemoryDomain.OPEN, "old binding closed new one");
        event(next, ManagedMemoryDomain.Pressure.NORMAL);
        next.beginClose(); waitPressure(next);
        d.beginClose(); waitDomain(d); d.forgetClosed();
        long[] after = ManagedMemoryDomain.runtimeCounts();
        check(after[0] == before[0] && after[1] == before[1] && after[2] == before[2], "binding pin leaked");
    }
    private static void expiryAndRebindBudget() throws Exception {
        ManagedMemoryDomain d = ManagedMemoryDomain.system(8192);
        ManagedMemoryDomain.Pressure p = d.attachPressure(4096, 1024);
        long first = event(p, ManagedMemoryDomain.Pressure.NORMAL)[2];
        for (int i = 0; i < 130; i++) event(p, ManagedMemoryDomain.Pressure.NORMAL);
        long last = event(p, ManagedMemoryDomain.Pressure.CRITICAL)[2];
        check(p.snapshot()[6] == 8192 && p.snapshot()[7] == 1024, "critical effective budget");
        p.beginClose(); waitPressure(p);
        check(p.eventResult(first)[0] == ManagedMemoryDomain.Pressure.EVENT_EXPIRED, "closed old event not expired");
        check(p.eventResult(last)[0] == ManagedMemoryDomain.Pressure.EVENT_APPLIED, "closed last event missing");
        check(d.snapshot()[2] == 1024, "detach unexpectedly raised budget");
        ManagedMemoryDomain.Pressure next = d.attachPressure(4096, 1024);
        check(next.snapshot()[6] == 1024 && next.snapshot()[7] == 1024, "rebind did not use current budget");
        event(next, ManagedMemoryDomain.Pressure.NORMAL);
        check(d.snapshot()[2] == 1024, "normal restored old manual target");
        d.setBudget(8192);
        check(d.snapshot()[2] == 8192, "explicit budget restore failed");
        d.beginClose(); waitDomain(d); d.forgetClosed();
    }
    private static void budgetGate() throws Exception {
        ManagedMemoryDomain d = ManagedMemoryDomain.system(8192);
        arm(12);
        AtomicReference<Throwable> failure = new AtomicReference<>();
        Thread setter = new Thread(() -> { try { d.setBudget(0); } catch (Throwable t) { failure.set(t); } });
        setter.start(); entered(12);
        try { d.attachPressure(4096, 1024); throw new AssertionError("attach crossed direct budget"); }
        catch (ManagedMemoryDomain.ManagedException e) { check(e.status() == 6, "attach gate status"); }
        release(12); setter.join(5000); check(!setter.isAlive() && failure.get() == null, "setter failed");
        ManagedMemoryDomain.Pressure p = d.attachPressure(4096, 1024);
        event(p, ManagedMemoryDomain.Pressure.NORMAL);
        check(p.snapshot()[7] == 0, "attach lost direct zero");
        d.beginClose(); waitDomain(d); check(p.state() == ManagedMemoryDomain.CLOSED, "domain failed to detach");
        d.forgetClosed();
    }
    private static void attachCloseGate() throws Exception {
        ManagedMemoryDomain d = ManagedMemoryDomain.system(8192);
        ManagedMemoryDomain.Pressure old = d.attachPressure(4096, 1024);
        long oldTicket = event(old, ManagedMemoryDomain.Pressure.NORMAL)[2];
        old.beginClose(); waitPressure(old);
        check(old.eventResult(oldTicket)[0] == ManagedMemoryDomain.Pressure.EVENT_APPLIED, "old result missing before attach");
        arm(11);
        AtomicReference<Throwable> result = new AtomicReference<>();
        Thread attach = new Thread(() -> { try { d.attachPressure(4096, 1024); }
            catch (Throwable t) { result.set(t); } });
        attach.start(); entered(11);
        check(old.eventResult(oldTicket) == null, "old result retained after attach attempt started");
        try { d.setBudget(0); throw new AssertionError("budget crossed attachment"); }
        catch (ManagedMemoryDomain.ManagedException e) { check(e.status() == 6, "budget gate status"); }
        d.beginClose();
        release(11); attach.join(5000);
        check(!attach.isAlive() && result.get() instanceof ManagedMemoryDomain.ManagedException &&
            ((ManagedMemoryDomain.ManagedException)result.get()).status() == 7, "attach/close result");
        waitDomain(d);
        check(old.eventResult(oldTicket) == null, "failed attach restored old generation");
        d.forgetClosed();
    }
    private static void manualEventRace() throws Exception {
        ManagedMemoryDomain d = ManagedMemoryDomain.system(8192);
        ManagedMemoryDomain.Pressure p = d.attachPressure(4096, 1024);
        CountDownLatch start = new CountDownLatch(1);
        AtomicReference<Throwable> error = new AtomicReference<>();
        Thread sender = new Thread(() -> {
            try {
                start.await();
                for (int i = 0; i < 64; i++) {
                    long result = p.submit(i % 2 == 0 ? ManagedMemoryDomain.Pressure.NORMAL : ManagedMemoryDomain.Pressure.CRITICAL);
                    if (result <= 0 && result != -6) throw new AssertionError("pressure submit: " + result);
                }
            } catch (Throwable t) { error.set(t); }
        });
        sender.start(); start.countDown(); d.setBudget(0); sender.join(5000);
        check(!sender.isAlive() && error.get() == null, "event race: " + error.get());
        event(p, ManagedMemoryDomain.Pressure.NORMAL);
        check(p.snapshot()[6] == 0 && p.snapshot()[7] == 0 && d.snapshot()[2] == 0,
            "concurrent restore raised manual zero");
        d.beginClose(); waitDomain(d); d.forgetClosed();
    }
    private static void concurrentClose() throws Exception {
        ManagedMemoryDomain d = ManagedMemoryDomain.arena(8192, 4096);
        ManagedMemoryDomain.Allocation owner = d.allocate(128, 8);
        ManagedMemoryDomain.Lease lease = owner.borrowRead(0, 128);
        ManagedMemoryDomain.Pressure p = d.attachPressure(4096, 1024);
        event(p, ManagedMemoryDomain.Pressure.CRITICAL);
        owner.close();
        CountDownLatch start = new CountDownLatch(1);
        AtomicReference<Throwable> error = new AtomicReference<>();
        Thread a = new Thread(() -> { try { start.await(); p.beginClose(); } catch (Throwable t) { error.set(t); } });
        Thread b = new Thread(() -> { try { start.await(); d.beginClose(); } catch (Throwable t) { error.set(t); } });
        a.start(); b.start(); start.countDown(); a.join(5000); b.join(5000);
        check(!a.isAlive() && !b.isAlive() && error.get() == null, "concurrent close failed: " + error.get());
        waitPressure(p);
        check(d.state() == ManagedMemoryDomain.CLOSING, "live lease did not defer domain close");
        lease.close(); waitDomain(d); d.forgetClosed();
    }
    private static void cancelledAfterClose() throws Exception {
        ManagedMemoryDomain d = ManagedMemoryDomain.system(8192);
        ManagedMemoryDomain.Pressure p = d.attachPressure(4096, 1024);
        check(nativePressureGate(0) == 1, "worker gate arm failed");
        long first = p.submit(ManagedMemoryDomain.Pressure.CRITICAL);
        check(first > 0 && nativePressureGate(1) == 1, "worker gate not entered");
        long cancelled = p.submit(ManagedMemoryDomain.Pressure.NORMAL);
        check(cancelled > 0, "queued ticket missing");
        p.beginClose(); d.beginClose();
        check(nativePressureGate(2) == 1, "pressure close not admitted");
        check(p.eventResult(cancelled) == null, "closing result should be unavailable");
        check(nativePressureGate(3) == 1, "worker gate release failed");
        waitPressure(p); waitDomain(d);
        long[] result = p.eventResult(cancelled);
        check(result != null && result[0] == ManagedMemoryDomain.Pressure.EVENT_CANCELLED &&
            result[1] == 7 && result[2] == cancelled, "closed cancelled result missing");
        check(p.eventResult(first)[0] == ManagedMemoryDomain.Pressure.EVENT_CANCELLED,
            "first queued event not cancelled");
        check(p.snapshot()[12] >= 2, "cancelled summary missing");
        d.forgetClosed();
        check(p.eventResult(cancelled) == null, "forgotten domain exposed result");
    }
    private static void lateJavaFailure() throws Exception {
        ManagedMemoryDomain d = ManagedMemoryDomain.system(8192);
        ManagedMemoryDomain.Pressure p = d.attachPressure(4096, 1024);
        arm(13); hook("nativeInject", 22);
        AtomicReference<Throwable> error = new AtomicReference<>();
        Thread submit = new Thread(() -> { try { p.submit(ManagedMemoryDomain.Pressure.CRITICAL); }
            catch (Throwable t) { error.set(t); } });
        submit.start(); entered(13);
        p.beginClose(); d.beginClose();
        long deadline = System.nanoTime() + 8_000_000_000L;
        long[] snap;
        do { snap = p.snapshot(); if (snap != null && snap[0] == 2) break; Thread.sleep(2); }
        while (System.nanoTime() < deadline);
        check(snap != null && snap[0] == 2, "native pressure cleanup did not finish");
        check(p.state() == ManagedMemoryDomain.CLOSING && d.state() == ManagedMemoryDomain.CLOSING,
            "late Java call prematurely reported closed");
        try { d.forgetClosed(); throw new AssertionError("forget admitted while JNI reporting"); }
        catch (ManagedMemoryDomain.ManagedException expected) { check(expected.status() == 6, "forget status"); }
        release(13); submit.join(5000);
        check(!submit.isAlive() && error.get() instanceof IllegalStateException &&
            error.get().getCause() instanceof java.io.IOException, "late JNI exception lost");
        check(p.state() == ManagedMemoryDomain.QUARANTINED && d.state() == ManagedMemoryDomain.QUARANTINED,
            "late JNI failure not quarantined");
    }
    public static void main(String[] args) throws Exception {
        zeroAndRebind(); expiryAndRebindBudget(); budgetGate(); attachCloseGate(); manualEventRace();
        concurrentClose(); cancelledAfterClose(); lateJavaFailure();
        System.out.println("managed pressure binding lifecycle passed");
    }
}
