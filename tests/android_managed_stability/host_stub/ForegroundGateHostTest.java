package io.github.jichuo1.meguviz.memory.android;
import android.app.Instrumentation;
import android.os.Bundle;

/** Host-only proof that missing or wrong prestarted identity cannot start work. */
public final class ForegroundGateHostTest {
    private static void require(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        Bundle options = new Bundle(); options.putString("run_id", "0123456789abcdef");
        options.putString("expected_pid", "222");
        ManagedStabilityInstrumentation probe = new ManagedStabilityInstrumentation();
        probe.onCreate(options); probe.onStart();
        require(Instrumentation.code == 1 && Instrumentation.result != null &&
            Instrumentation.result.getString("report_json").contains("\"status\":\"INCOMPLETE\""));
        PressureR8ForegroundActivity.current = new PressureR8ForegroundActivity();
        options.putString("expected_pid", "111"); options.putString("phase_timeout_ms", "1000");
        options.putString("interval_ms", "1"); options.putString("run_id", String.format("%016x", System.nanoTime()));
        System.setProperty("test.stability.preparation.delay", "1100");
        probe = new ManagedStabilityInstrumentation(); probe.onCreate(options); probe.onStart();
        require(Instrumentation.code == 1 && Instrumentation.result.getString("report_json").contains("\"reason\":\"preparation_timeout\""));
        long start = 1_000_000_000L, prepEnd = start + 100_000_000_000L;
        require(!StabilityTiming.qualified(900_000, prepEnd, prepEnd + 899_000_000_000L, 1));
        require(StabilityTiming.qualified(900_000, prepEnd, prepEnd + 900_000_000_000L, 1));
        require(ManagedMemoryDomain.runtimeCounts()[0] == 0);
        System.out.println("{\"suite\":\"managed-stability-host-gate\",\"status\":\"PASS\"}");
    }
}
