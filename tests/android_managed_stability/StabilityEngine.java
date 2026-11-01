package io.github.jichuo1.meguviz.memory.android;

/** Real managed-domain operations; the observer owns identity, timeouts and progress. */
final class StabilityEngine {
    interface Observer { void phase(String phase, int iteration) throws Exception; }
    interface Guard { void check() throws Exception; }
    interface AfterCall { void reached(String name) throws Exception; }
    interface Call<T> { T run() throws Exception; }
    interface Action { void run() throws Exception; }
    static final class Incomplete extends Exception { Incomplete() { super(); } }
    private final Observer observer;
    private final Guard guard;
    private final AfterCall afterCall;
    StabilityEngine(Observer observer, Guard guard) { this(observer, guard, name -> {}); }
    StabilityEngine(Observer observer, Guard guard, AfterCall afterCall) {
        this.observer = observer; this.guard = guard; this.afterCall = afterCall;
    }
    private <T> T call(String name, Call<T> operation) throws Exception {
        guard.check();
        try { T result = operation.run(); afterCall.reached(name); return result; }
        finally { guard.check(); }
    }
    private void act(String name, Action operation) throws Exception {
        call(name, () -> { operation.run(); return null; });
    }
    private void phase(String name, int iteration) throws Exception { observer.phase(name, iteration); }
    private static void require(boolean value) { if (!value) throw new AssertionError(); }
    private void event(ManagedMemoryDomain.Pressure p, int level) throws Exception {
        long deadline = System.nanoTime() + 5_000_000_000L, ticket = -6;
        while (ticket == -6 && System.nanoTime() < deadline) {
            ticket = call("pressure_submit", () -> p.submit(level));
            if (ticket == -6) Thread.sleep(2);
        }
        if (ticket <= 0) throw new Incomplete();
        long[] result;
        do {
            final long accepted = ticket;
            result = call("event_result", () -> p.eventResult(accepted));
            if (result == null || result.length != 4 || result[2] != ticket) throw new Incomplete();
            if (result[0] != ManagedMemoryDomain.Pressure.EVENT_QUEUED) break;
            Thread.sleep(2);
        } while (System.nanoTime() < deadline);
        if (result[0] == ManagedMemoryDomain.Pressure.EVENT_QUEUED) throw new Incomplete();
        require(result[0] == ManagedMemoryDomain.Pressure.EVENT_APPLIED && result[1] == 0 && result[3] > 0);
    }
    private void waitPressure(ManagedMemoryDomain.Pressure p) throws Exception {
        long deadline = System.nanoTime() + 8_000_000_000L;
        while (call("pressure_state", p::state) == ManagedMemoryDomain.CLOSING && System.nanoTime() < deadline) Thread.sleep(2);
        if (call("pressure_state", p::state) != ManagedMemoryDomain.CLOSED) throw new Incomplete();
    }
    private void waitDomain(ManagedMemoryDomain d) throws Exception {
        long deadline = System.nanoTime() + 8_000_000_000L;
        while (call("domain_state", d::state) == ManagedMemoryDomain.CLOSING && System.nanoTime() < deadline) Thread.sleep(2);
        if (call("domain_state", d::state) != ManagedMemoryDomain.CLOSED) throw new Incomplete();
    }
    void cycle(boolean arena, int iteration) throws Exception {
        phase(arena ? "arena_create" : "system_create", iteration);
        ManagedMemoryDomain d = call("domain_create", () -> arena ? ManagedMemoryDomain.arena(8192, 4096) : ManagedMemoryDomain.system(8192));
        ManagedMemoryDomain.Allocation owner = call("allocate", () -> d.allocate(128, 8));
        ManagedMemoryDomain.Lease lease = call("borrow", () -> owner.borrowWrite(0, 128));
        byte[] bytes = new byte[128]; act("lease_read", () -> lease.read(0, bytes, 0, bytes.length));
        for (byte value : bytes) require(value == 0);
        act("lease_write", () -> lease.write(0, new byte[]{17, 29}, 0, 2));
        ManagedMemoryDomain.Pressure p = call("attach_pressure", () -> d.attachPressure(4096, 1024));
        phase(arena ? "arena_pressure" : "system_pressure", iteration);
        event(p, ManagedMemoryDomain.Pressure.CRITICAL);
        long[] ps = call("pressure_snapshot", p::snapshot), ds = call("domain_snapshot", d::snapshot);
        require(ps != null && ps.length >= 20 && ps[6] == 8192 && ps[7] == 1024 && ds != null && ds[2] == 1024);
        act("set_budget", () -> d.setBudget(0)); event(p, ManagedMemoryDomain.Pressure.NORMAL);
        ps = call("pressure_snapshot", p::snapshot); ds = call("domain_snapshot", d::snapshot);
        require(ps != null && ps[6] == 0 && ps[7] == 0 && ds != null && ds[2] == 0);
        try { call("budget_rejection", () -> d.allocate(1, 8)); throw new AssertionError(); }
        catch (ManagedMemoryDomain.ManagedException expected) { require(expected.status() == 4); }
        phase(arena ? "arena_owner_release" : "system_owner_release", iteration);
        act("owner_close", owner::close);
        act("domain_begin_close", d::beginClose); act("domain_begin_close_repeat", d::beginClose);
        phase(arena ? "arena_held_lease" : "system_held_lease", iteration);
        waitPressure(p);
        require(call("domain_state", d::state) == ManagedMemoryDomain.CLOSING);
        ds = call("domain_snapshot", d::snapshot); require(ds != null && ds[3] > 0 && ds[7] == 128 && ds[8] == 1 && ds[9] == 1);
        byte[] actual = new byte[2]; act("lease_read", () -> lease.read(0, actual, 0, 2));
        require(actual[0] == 17 && actual[1] == 29);
        act("lease_write", () -> lease.write(1, new byte[]{31}, 0, 1));
        act("lease_read", () -> lease.read(1, actual, 0, 1)); require(actual[0] == 31);
        phase(arena ? "arena_lease_release" : "system_lease_release", iteration);
        act("lease_close", lease::close); act("lease_close_repeat", lease::close); waitDomain(d);
        ds = call("domain_snapshot", d::snapshot);
        require(ds != null && ds[3] == 0 && ds[4] == 0 && ds[6] == 0 && ds[7] == 0 && ds[8] == 0 && ds[9] == 0);
        act("forget_closed", d::forgetClosed);
        phase(arena ? "arena_zero" : "system_zero", iteration);
    }
}
