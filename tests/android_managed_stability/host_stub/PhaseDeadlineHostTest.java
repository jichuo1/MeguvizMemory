package io.github.jichuo1.meguviz.memory.android;

import android.util.AtomicFile;
import java.io.File;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.concurrent.atomic.AtomicLong;

/** Fake monotonic clock, no watchdog scheduling: expired boundaries must stop. */
public final class PhaseDeadlineHostTest {
    private static void require(boolean value) { if (!value) throw new AssertionError(); }
    private static void set(Object target, String field, Object value) throws Exception {
        Field f = ManagedStabilityInstrumentation.class.getDeclaredField(field); f.setAccessible(true); f.set(target, value);
    }
    private static Object get(Object target, String field) throws Exception {
        Field f = ManagedStabilityInstrumentation.class.getDeclaredField(field); f.setAccessible(true); return f.get(target);
    }
    private static void run(String methodName, boolean preparation) throws Exception {
        AtomicLong now = new AtomicLong(3999);
        ManagedStabilityInstrumentation probe = new ManagedStabilityInstrumentation(now::get);
        PressureR8ForegroundActivity.current = new PressureR8ForegroundActivity();
        set(probe, "foreground", PressureR8ForegroundActivity.current); set(probe, "expectedPid", 111);
        set(probe, "initialPauseCount", 0); set(probe, "started", 1000L);
        set(probe, "workloadStarted", preparation ? 0L : 2000L);
        set(probe, "idleNanos", 1000L); set(probe, "phaseAt", 3000L); set(probe, "phase", "system_owner_release");
        set(probe, "sequence", 7L); set(probe, "iteration", 4); set(probe, "runId", "0123456789abcdef");
        File base = new File(System.getProperty("test.stability.cache"), "deadline-" + methodName + "-" + System.nanoTime());
        File stall = new File(base.getPath() + "-stall.json");
        set(probe, "progress", new AtomicFile(new File(base.getPath() + "-progress.json")));
        set(probe, "stall", new AtomicFile(stall));
        Method method = methodName.equals("publish") ? ManagedStabilityInstrumentation.class.getDeclaredMethod(methodName, String.class, int.class)
            : ManagedStabilityInstrumentation.class.getDeclaredMethod(methodName);
        method.setAccessible(true);
        Method check = ManagedStabilityInstrumentation.class.getDeclaredMethod("checkActive"); check.setAccessible(true);
        check.invoke(probe); // Before the deadline, no watchdog is required to proceed.
        now.set(4000); // Exactly at the deadline; no watchdog thread is running.
        try { if (methodName.equals("publish")) method.invoke(probe, "system_held_lease", 4); else method.invoke(probe); throw new AssertionError(); }
        catch (InvocationTargetException error) { require(error.getCause() instanceof StabilityEngine.Incomplete); }
        require((Boolean)get(probe, "unknown") && ((Long)get(probe, "sequence")) == 7L &&
            get(probe, "phase").equals("system_owner_release") && ((Long)get(probe, "phaseAt")) == 3000L);
        String report = new String(Files.readAllBytes(stall.toPath()), StandardCharsets.UTF_8);
        require(report.contains("\"status\":\"INCOMPLETE\"") && report.contains(preparation ?
            "\"reason\":\"preparation_timeout\"" : "\"reason\":\"phase_timeout_unknown\""));
    }
    private static void crossingDuringPublish() throws Exception {
        java.util.concurrent.atomic.AtomicInteger reads = new java.util.concurrent.atomic.AtomicInteger();
        ManagedStabilityInstrumentation probe = new ManagedStabilityInstrumentation(() ->
            reads.getAndIncrement() == 0 ? 3999 : 5001);
        PressureR8ForegroundActivity.current = new PressureR8ForegroundActivity();
        set(probe, "foreground", PressureR8ForegroundActivity.current); set(probe, "expectedPid", 111);
        set(probe, "initialPauseCount", 0); set(probe, "started", 1000L); set(probe, "workloadStarted", 2000L);
        set(probe, "idleNanos", 1000L); set(probe, "phaseAt", 3000L); set(probe, "phase", "system_owner_release");
        set(probe, "sequence", 7L); set(probe, "iteration", 4); set(probe, "runId", "0123456789abcdef");
        File base = new File(System.getProperty("test.stability.cache"), "crossing-" + System.nanoTime());
        File stall = new File(base.getPath() + "-stall.json");
        set(probe, "progress", new AtomicFile(new File(base.getPath() + "-progress.json")));
        set(probe, "stall", new AtomicFile(stall));
        Method method = ManagedStabilityInstrumentation.class.getDeclaredMethod("publish", String.class, int.class);
        method.setAccessible(true);
        try { method.invoke(probe, "system_held_lease", 4); throw new AssertionError(); }
        catch (InvocationTargetException error) { require(error.getCause() instanceof StabilityEngine.Incomplete); }
        require((Boolean)get(probe, "unknown") && ((Long)get(probe, "phaseAt")) == 3999L);
        String report = new String(Files.readAllBytes(stall.toPath()), StandardCharsets.UTF_8);
        require(report.contains("\"reason\":\"phase_timeout_unknown\""));
    }
    public static void main(String[] args) throws Exception {
        run("checkActive", false); run("publish", false); run("checkActive", true); crossingDuringPublish();
        System.out.println("{\"suite\":\"managed-stability-host-deadline\",\"status\":\"PASS\"}");
    }
}
