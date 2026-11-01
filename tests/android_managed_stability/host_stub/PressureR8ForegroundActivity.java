package io.github.jichuo1.meguviz.memory.android;
/** Never asserts a foreground identity in host-only tests. */
public class PressureR8ForegroundActivity {
    static PressureR8ForegroundActivity current;
    boolean executeImmediately = true;
    boolean failFinish;
    Runnable queued;
    Runnable beforeRun;
    int finishes;
    static PressureR8ForegroundActivity currentForDiagnostic() { return current; }
    boolean isResumedForDiagnostic() { return true; }
    int pauseCountForDiagnostic() { return 0; }
    public void runOnUiThread(Runnable task) {
        if (beforeRun != null) beforeRun.run();
        if (executeImmediately) task.run(); else queued = task;
    }
    public void finish() {
        if (failFinish) throw new IllegalStateException();
        ++finishes;
    }
}
