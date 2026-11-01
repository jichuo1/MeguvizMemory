package io.github.jichuo1.meguviz.memory.android;

import java.lang.reflect.Field;

/** A finish request targets only the confirmed Activity and never an unknown domain. */
public final class ForegroundFinishHostTest {
    private static void require(boolean value) { if (!value) throw new AssertionError(); }
    private static void set(Object target, String name, Object value) throws Exception {
        Field field = ManagedStabilityInstrumentation.class.getDeclaredField(name);
        field.setAccessible(true); field.set(target, value);
    }
    private static ManagedStabilityInstrumentation probe(PressureR8ForegroundActivity activity) throws Exception {
        ManagedStabilityInstrumentation result = new ManagedStabilityInstrumentation();
        PressureR8ForegroundActivity.current = activity;
        set(result, "foreground", activity); set(result, "expectedPid", 111);
        set(result, "initialPauseCount", 0);
        return result;
    }
    public static void main(String[] args) throws Exception {
        PressureR8ForegroundActivity target = new PressureR8ForegroundActivity();
        ManagedStabilityInstrumentation test = probe(target);
        require(test.requestOwnForegroundFinish(100) && target.finishes == 1);

        target = new PressureR8ForegroundActivity(); test = probe(target);
        set(test, "activeDomainPossible", true);
        require(!test.requestOwnForegroundFinish(100) && target.finishes == 0 && target.queued == null);
        set(test, "activeDomainPossible", false); set(test, "unknown", true);
        require(!test.requestOwnForegroundFinish(100) && target.finishes == 0);

        target = new PressureR8ForegroundActivity(); test = probe(target);
        set(test, "expectedPid", 222);
        require(!test.requestOwnForegroundFinish(100) && target.finishes == 0);

        target = new PressureR8ForegroundActivity(); test = probe(target);
        target.executeImmediately = false;
        require(!test.requestOwnForegroundFinish(1) && target.queued != null);
        target.queued.run(); // A delayed UI task must not act after timeout.
        require(target.finishes == 0);

        target = new PressureR8ForegroundActivity(); test = probe(target);
        target.beforeRun = () -> PressureR8ForegroundActivity.current = new PressureR8ForegroundActivity();
        require(!test.requestOwnForegroundFinish(100) && target.finishes == 0);

        target = new PressureR8ForegroundActivity(); test = probe(target);
        final ManagedStabilityInstrumentation changed = test;
        target.beforeRun = () -> {
            try { set(changed, "activeDomainPossible", true); }
            catch (Exception error) { throw new IllegalStateException(error); }
        };
        require(!test.requestOwnForegroundFinish(100) && target.finishes == 0);

        target = new PressureR8ForegroundActivity(); test = probe(target); target.failFinish = true;
        require(!test.requestOwnForegroundFinish(100) && target.finishes == 0);
        System.out.println("{\"suite\":\"managed-stability-host-foreground-finish\",\"status\":\"PASS\"}");
    }
}
