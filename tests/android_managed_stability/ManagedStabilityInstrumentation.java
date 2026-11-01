package io.github.jichuo1.meguviz.memory.android;

import android.app.Instrumentation;
import android.os.Bundle;
import android.os.Looper;
import android.os.Process;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileOutputStream;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.json.JSONObject;

/** Own-process, prestarted foreground diagnostic; no system policy changes. */
public final class ManagedStabilityInstrumentation extends Instrumentation {
    private static final class PreparationTimeout extends Exception {}
    interface Clock { long nanos(); }
    private final Clock clock;
    public ManagedStabilityInstrumentation() { this(System::nanoTime); }
    ManagedStabilityInstrumentation(Clock clock) { this.clock = clock; }
    private Bundle arguments;
    private final Object state = new Object();
    private volatile boolean unknown, done;
    private volatile boolean activeDomainPossible;
    private volatile String stallReason = "phase_timeout_unknown";
    private volatile String phase = "created";
    private volatile long phaseAt, sequence;
    private volatile int iteration;
    private long started, idleNanos;
    private volatile long workloadStarted;
    private String runId;
    private AtomicFile progress, stall;
    private PressureR8ForegroundActivity foreground;
    private int expectedPid, initialPauseCount;

    @Override public void onCreate(Bundle args) { super.onCreate(args); arguments = args; start(); }
    private static long option(Bundle args, String key, long fallback, long min, long max) {
        String raw = args == null ? null : args.getString(key);
        long value = raw == null ? fallback : Long.parseLong(raw);
        if (value < min || value > max) throw new IllegalArgumentException();
        return value;
    }
    private boolean identity() {
        return expectedPid > 0 && Process.myPid() == expectedPid && foreground != null &&
            PressureR8ForegroundActivity.currentForDiagnostic() == foreground &&
            foreground.isResumedForDiagnostic() && foreground.pauseCountForDiagnostic() == initialPauseCount;
    }
    private JSONObject record(String status, String reason) throws Exception {
        return record(status, reason, identity());
    }
    private JSONObject record(String status, String reason, boolean foregroundConfirmed) throws Exception {
        JSONObject result = new JSONObject();
        result.put("suite", "android-managed-stability-v1"); result.put("status", status);
        result.put("run_id", runId); result.put("pid", Process.myPid());
        result.put("sequence", sequence); result.put("phase", phase); result.put("iteration", iteration);
        result.put("elapsed_ms", Math.max(0, (clock.nanos() - started) / 1_000_000L));
        long workload = workloadStarted;
        result.put("workload_elapsed_ms", workload == 0 ? 0 : Math.max(0, (clock.nanos() - workload) / 1_000_000L));
        result.put("snapshot_consistency", "non_atomic");
        result.put("foreground_confirmed", foregroundConfirmed); result.put("reason", reason);
        result.put("pressure_origin", "synthetic"); result.put("performance_qualified", false);
        return result;
    }
    private static void save(AtomicFile target, JSONObject value) throws Exception {
        byte[] bytes = (value.toString() + "\n").getBytes("UTF-8");
        if (bytes.length > 8192) throw new IllegalStateException();
        FileOutputStream stream = null;
        try { stream = target.startWrite(); stream.write(bytes); target.finishWrite(stream); }
        catch (Throwable error) { if (stream != null) target.failWrite(stream); throw error; }
    }
    // Caller holds state. Once expired, no later phase may erase the evidence.
    private boolean markExpiredLocked(long now) {
        if (!done && !unknown && idleNanos > 0 && StabilityTiming.expired(phaseAt, now, idleNanos)) {
            stallReason = workloadStarted == 0 ? "preparation_timeout" : "phase_timeout_unknown";
            unknown = true;
            return true;
        }
        return false;
    }
    private void emitStall() {
        // The first thread marking unknown owns this write; never wait on an in-flight call.
        try { if (stall != null) save(stall, record("INCOMPLETE", stallReason)); }
        catch (Throwable ignored) { /* missing terminal/stall remains externally incomplete */ }
    }
    private void publish(String next, int round) throws Exception {
        boolean expired, allowed;
        synchronized (state) {
            long now = clock.nanos();
            expired = markExpiredLocked(now);
            allowed = !unknown && identity();
            if (allowed) { phase = next; iteration = round; ++sequence; phaseAt = now; }
        }
        if (expired) emitStall();
        if (!allowed) throw new StabilityEngine.Incomplete();
        save(progress, record("RUNNING", "none"));
        checkActive();
    }
    private void checkActive() throws StabilityEngine.Incomplete {
        boolean expired, allowed;
        synchronized (state) { expired = markExpiredLocked(clock.nanos()); allowed = !unknown && identity(); }
        if (expired) emitStall();
        if (!allowed) throw new StabilityEngine.Incomplete();
    }
    private CountDownLatch startWatchdog() {
        CountDownLatch ready = new CountDownLatch(1);
        Thread watchdog = new Thread(() -> {
            try {
                ready.countDown();
                while (!done && !unknown) {
                    Thread.sleep(100);
                    boolean expired;
                    synchronized (state) {
                        expired = markExpiredLocked(clock.nanos());
                    }
                    if (expired) {
                        // Separate file, no lock or join on the potentially blocked worker.
                        emitStall();
                        return;
                    }
                }
            } catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
        }, "managed-stability-watchdog");
        watchdog.setDaemon(true); watchdog.start();
        return ready;
    }
    // Only this prestarted Activity is targeted. A queued request is revoked on
    // timeout; an unknown domain must never be disturbed to manufacture exit.
    boolean requestOwnForegroundFinish(long timeoutMillis) throws InterruptedException {
        final PressureR8ForegroundActivity target = foreground;
        if (timeoutMillis <= 0 || unknown || activeDomainPossible || !identity()) return false;
        final AtomicInteger request = new AtomicInteger(0); // pending, executing, revoked, sent, failed
        final CountDownLatch complete = new CountDownLatch(1);
        try {
            target.runOnUiThread(() -> {
                if (!request.compareAndSet(0, 1)) return;
                try {
                    if (!unknown && !activeDomainPossible && identity() && foreground == target) {
                        target.finish();
                        request.set(3);
                    } else request.set(4);
                } catch (Throwable error) { request.set(4); }
                finally { complete.countDown(); }
            });
        } catch (Throwable error) {
            request.compareAndSet(0, 2);
            return false;
        }
        try {
            if (!complete.await(timeoutMillis, TimeUnit.MILLISECONDS)) {
                request.compareAndSet(0, 2);
                return false;
            }
        } catch (InterruptedException interrupted) {
            request.compareAndSet(0, 2);
            throw interrupted;
        }
        return request.get() == 3;
    }
    @Override public void onStart() {
        started = clock.nanos(); phaseAt = started;
        Bundle result = new Bundle();
        try {
            if (Looper.myLooper() == Looper.getMainLooper()) throw new StabilityEngine.Incomplete();
            runId = arguments == null ? null : arguments.getString("run_id");
            if (runId == null || !runId.matches("[0-9a-f]{16}")) throw new StabilityEngine.Incomplete();
            expectedPid = (int)option(arguments, "expected_pid", 0, 1, Integer.MAX_VALUE);
            long duration = option(arguments, "duration_ms", 900_000, 1000, 3_600_000);
            int maxRounds = (int)option(arguments, "max_rounds", 10000, 1, 100000);
            long interval = option(arguments, "interval_ms", 250, 1, 60000);
            idleNanos = option(arguments, "phase_timeout_ms", 30000, 1000, 60000) * 1_000_000L;
            if (interval * 1_000_000L >= idleNanos) throw new StabilityEngine.Incomplete();
            foreground = PressureR8ForegroundActivity.currentForDiagnostic();
            if (foreground == null) throw new StabilityEngine.Incomplete();
            initialPauseCount = foreground.pauseCountForDiagnostic();
            if (!identity()) throw new StabilityEngine.Incomplete();
            File cache = getTargetContext().getCacheDir();
            progress = new AtomicFile(new File(cache, "managed-stability-" + runId + "-progress.json"));
            stall = new AtomicFile(new File(cache, "managed-stability-" + runId + "-stall.json"));
            // Reusing an ID could confuse an old stall with this run.
            for (AtomicFile file : new AtomicFile[]{progress, stall}) {
                File base = file.getBaseFile();
                if (base.exists() || new File(base.getPath() + ".bak").exists() ||
                    new File(base.getPath() + ".new").exists()) throw new StabilityEngine.Incomplete();
            }
            if (StabilityTiming.expired(started, clock.nanos(), idleNanos)) throw new PreparationTimeout();
            publish("starting", 0);
            CountDownLatch ready = startWatchdog();
            if (!ready.await(Math.min(3000, idleNanos / 1_000_000L), TimeUnit.MILLISECONDS) ||
                StabilityTiming.expired(started, clock.nanos(), idleNanos)) throw new PreparationTimeout();
            checkActive();
            workloadStarted = clock.nanos();
            publish("workload_start", 0);
            StabilityEngine engine = new StabilityEngine(this::publish, this::checkActive);
            int rounds = 0;
            while (rounds < maxRounds && clock.nanos() - workloadStarted < duration * 1_000_000L) {
                int current = rounds + 1;
                activeDomainPossible = true;
                engine.cycle(false, current); engine.cycle(true, current);
                activeDomainPossible = false;
                rounds = current; publish("round_complete", rounds);
                if (clock.nanos() - workloadStarted < duration * 1_000_000L) {
                    publish("interval", rounds); Thread.sleep(interval);
                }
            }
            publish("complete", rounds);
            boolean expired, allowed;
            synchronized (state) {
                expired = markExpiredLocked(clock.nanos()); allowed = !unknown && identity();
                if (allowed) done = true;
            }
            if (expired) emitStall();
            if (!allowed) throw new StabilityEngine.Incomplete();
            boolean foregroundConfirmed = identity();
            boolean qualified = foregroundConfirmed && StabilityTiming.qualified(duration, workloadStarted, clock.nanos(), rounds);
            boolean finishRequested = requestOwnForegroundFinish(3000);
            JSONObject report = record(qualified && finishRequested ? "PASS" : "INCOMPLETE",
                !finishRequested ? "foreground_cleanup_unknown" : qualified ? "none" : "duration_or_round_limit",
                foregroundConfirmed);
            save(progress, report); result.putString("report_json", report.toString()); finish(qualified && finishRequested ? 0 : 1, result);
        } catch (Throwable error) {
            synchronized (state) { done = true; }
            if (unknown) return; // Worker status was unknown; watchdog owns the stall evidence. Never force cleanup.
            try {
                String status = error instanceof AssertionError ? "FAIL" : "INCOMPLETE";
                String reason = error instanceof PreparationTimeout ? "preparation_timeout" :
                    error instanceof StabilityEngine.Incomplete ? "identity_or_timeout" : "operation_failed";
                JSONObject report = record(status, reason);
                if (progress != null) save(progress, report);
                result.putString("report_json", report.toString());
            } catch (Throwable ignored) {
                result.putString("report_json", "{\"suite\":\"android-managed-stability-v1\",\"status\":\"INCOMPLETE\",\"reason\":\"report_unavailable\"}");
            }
            // The runner must not terminate an instrumentation with a possibly
            // live domain after an uncertain operation; external observation
            // decides the next step. No best-effort close is attempted here.
            if (!activeDomainPossible) {
                try { requestOwnForegroundFinish(3000); }
                catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
                finish(1, result);
            }
        }
    }
}
