package io.github.jichuo1.meguviz.memory.image;

/** Deterministic admission tests; this is not a native or Android test. */
public final class PressureRetryJvmTest {
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    public static void main(String[] args) throws Exception {
        int[] attempts={0}, busy={0};
        long ticket=JavaImage.submitPressureWithRetry(
            () -> ++attempts[0] <= 2 ? -6 : 41,
            () -> ++busy[0], System.nanoTime()+1_000_000_000L);
        check(ticket==41 && attempts[0]==3 && busy[0]==2, "BUSY retry or accepted ticket");

        attempts[0]=busy[0]=0;
        try {
            JavaImage.submitPressureWithRetry(() -> { ++attempts[0]; return -4; },
                () -> ++busy[0], System.nanoTime()+1_000_000_000L);
            throw new AssertionError("non-BUSY rejection accepted");
        } catch (IllegalStateException expected) {
            check(attempts[0]==1 && busy[0]==0, "non-BUSY rejection retried");
        }

        attempts[0]=busy[0]=0;
        try {
            JavaImage.submitPressureWithRetry(() -> { ++attempts[0]; return 1; },
                () -> ++busy[0], System.nanoTime()-1);
            throw new AssertionError("expired deadline accepted");
        } catch (IllegalStateException expected) {
            check(attempts[0]==0 && busy[0]==0, "submission after deadline");
        }

        attempts[0]=busy[0]=0;
        Thread.currentThread().interrupt();
        try {
            JavaImage.submitPressureWithRetry(() -> { ++attempts[0]; return -6; },
                () -> ++busy[0], System.nanoTime()+1_000_000_000L);
            throw new AssertionError("interrupt ignored");
        } catch (InterruptedException expected) {
            check(attempts[0]==1 && busy[0]==1, "interrupted BUSY accounting");
        } finally { Thread.interrupted(); }
        System.out.println("PASS_PRESSURE_RETRY_ADMISSION_ONLY");
    }
}
