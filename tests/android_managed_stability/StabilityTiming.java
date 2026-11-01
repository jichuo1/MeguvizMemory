package io.github.jichuo1.meguviz.memory.android;

/** Monotonic clocks only; preparation never contributes to workload duration. */
final class StabilityTiming {
    private StabilityTiming() {}
    static boolean expired(long began, long now, long limitNanos) { return now - began >= limitNanos; }
    static boolean qualified(long requestedMillis, long workloadStarted, long now, int rounds) {
        return requestedMillis >= 900_000 && workloadStarted != 0 && rounds > 0 &&
            now - workloadStarted >= requestedMillis * 1_000_000L;
    }
}
