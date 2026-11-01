package io.github.jichuo1.meguviz.memory.android;

/** Host-only real core/JNI regression; Android runtime is not involved. */
public final class EngineHostTest {
    private static void require(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) throws Exception {
        long[] before = ManagedMemoryDomain.runtimeCounts();
        if (args.length == 1 && args[0].equals("unknown")) {
            StabilityEngine engine = new StabilityEngine((stage, iteration) -> {
                if (stage.equals("system_held_lease")) throw new StabilityEngine.Incomplete();
            }, () -> {});
            try { engine.cycle(false, 1); throw new AssertionError(); }
            catch (StabilityEngine.Incomplete expected) { /* deliberately retain until process exit */ }
            long[] after = ManagedMemoryDomain.runtimeCounts();
            // Pressure teardown may still hold a separate pin; sampling is not atomic.
            require(after[0] == before[0] + 1 && after[2] >= before[2] + 1);
            System.out.println("{\"suite\":\"managed-stability-host-unknown\",\"status\":\"PASS\",\"forced_cleanup\":false}");
            return;
        }
        int[] phases = {0};
        if (args.length == 1 && (args[0].equals("owner_timeout") || args[0].equals("lease_timeout"))) {
            String target = args[0].equals("owner_timeout") ? "owner_close" : "lease_close";
            java.util.concurrent.atomic.AtomicBoolean unknown = new java.util.concurrent.atomic.AtomicBoolean();
            java.util.concurrent.CountDownLatch entered = new java.util.concurrent.CountDownLatch(1);
            java.util.concurrent.CountDownLatch returnAllowed = new java.util.concurrent.CountDownLatch(1);
            java.util.concurrent.atomic.AtomicReference<Throwable> observed = new java.util.concurrent.atomic.AtomicReference<>();
            java.util.List<String> calls = new java.util.ArrayList<>();
            StabilityEngine engine = new StabilityEngine((stage, iteration) -> {},
                () -> { if (unknown.get()) throw new StabilityEngine.Incomplete(); },
                name -> {
                    calls.add(name);
                    if (name.equals(target)) {
                        entered.countDown();
                        if (!returnAllowed.await(3, java.util.concurrent.TimeUnit.SECONDS)) throw new AssertionError();
                    }
                });
            Thread worker = new Thread(() -> {
                try { engine.cycle(false, 1); observed.set(new AssertionError()); }
                catch (Throwable result) { observed.set(result); }
            });
            worker.start();
            try {
                require(entered.await(3, java.util.concurrent.TimeUnit.SECONDS));
                unknown.set(true); // The wrapped state call has not returned to the cycle yet.
            } finally { returnAllowed.countDown(); }
            worker.join(4000);
            require(!worker.isAlive() && observed.get() instanceof StabilityEngine.Incomplete &&
                unknown.get() && calls.get(calls.size() - 1).equals(target));
            require(!calls.contains(args[0].equals("owner_timeout") ? "domain_begin_close" : "lease_close_repeat"));
            System.out.println("{\"suite\":\"managed-stability-host-boundary\",\"status\":\"PASS\"}");
            return;
        }
        StabilityEngine engine = new StabilityEngine((stage, iteration) -> {
            require(iteration >= 1 && stage.matches("(system|arena)_[a-z_]+")); ++phases[0];
        }, () -> {});
        for (int i = 1; i <= 2; i++) { engine.cycle(false, i); engine.cycle(true, i); }
        long[] after = ManagedMemoryDomain.runtimeCounts();
        require(phases[0] == 24 && after[0] == before[0] && after[1] == before[1] && after[2] == before[2]);
        System.out.println("{\"suite\":\"managed-stability-host-normal\",\"status\":\"PASS\",\"cycles\":4}");
    }
}
