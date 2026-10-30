package io.github.jichuo1.meguviz.memory.android;

import android.app.Activity;
import android.os.Bundle;
import android.view.WindowManager;
import android.widget.TextView;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/** Optional, own-process foreground surface for a bounded diagnostic run. */
public final class PressureR8ForegroundActivity extends Activity {
    private static volatile PressureR8ForegroundActivity current;
    private final CountDownLatch firstResume = new CountDownLatch(1);
    private volatile CountDownLatch stoppedOrDestroyed = new CountDownLatch(1);
    private final AtomicInteger pauseCount = new AtomicInteger();
    private volatile boolean resumed;

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        current = this;
        // API 27 is the diagnostic APK's minimum. These flags are scoped to
        // this Activity/window and do not modify global device policy.
        setShowWhenLocked(true);
        setTurnScreenOn(true);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        TextView message = new TextView(this);
        message.setText("MeguvizMemory 正在进行内存安全测试");
        message.setTextSize(18);
        int padding = (int)(24 * getResources().getDisplayMetrics().density);
        message.setPadding(padding, padding, padding, padding);
        setContentView(message);
    }

    @Override protected void onResume() {
        super.onResume();
        resumed = true;
        firstResume.countDown();
    }

    @Override protected void onPause() {
        if (resumed) pauseCount.incrementAndGet();
        resumed = false;
        super.onPause();
    }

    @Override protected void onStop() {
        super.onStop();
        stoppedOrDestroyed.countDown();
    }

    @Override protected void onDestroy() {
        super.onDestroy();
        if (current == this) current = null;
        stoppedOrDestroyed.countDown();
    }

    static PressureR8ForegroundActivity currentForDiagnostic() { return current; }

    boolean awaitFirstResume(long timeoutMillis) throws InterruptedException {
        return firstResume.await(timeoutMillis, TimeUnit.MILLISECONDS);
    }
    boolean isResumedForDiagnostic() { return resumed; }
    int pauseCountForDiagnostic() { return pauseCount.get(); }
    void prepareStopObservation() { stoppedOrDestroyed = new CountDownLatch(1); }
    boolean awaitStoppedOrDestroyed(long timeoutMillis) throws InterruptedException {
        return stoppedOrDestroyed.await(timeoutMillis, TimeUnit.MILLISECONDS);
    }
}
