package io.github.jichuo1.meguviz.memory.android;

import android.app.Application;
import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.util.AtomicFile;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.locks.ReentrantLock;
import org.json.JSONArray;
import org.json.JSONObject;

/** Bounded real AAR/R8 consumer. This never exhausts device RAM or forces GC. */
public final class PressureR8Instrumentation extends Instrumentation {
    private Bundle arguments;
    private volatile int checks, completed;
    private long deadline, peakBacking;
    private long started;
    private volatile boolean unsafeCleanup, finished;
    private boolean injectMainRunningTimeout, injectProgressLockTimeout, captureStackOnHalt;
    private boolean foregroundRequested, foregroundPrestarted;
    private volatile boolean foregroundResumed, foregroundFinished;
    private volatile PressureR8ForegroundActivity foregroundActivity;
    private volatile String fireAndForgetState = "NOT_STARTED";
    private volatile long fireAndForgetElapsedMillis;
    private volatile int fireAndForgetPolls, foregroundChecks;
    private int foregroundPauseCountBeforeFinish, foregroundInitialPauseCount;
    private long stallTimeoutMillis;
    private volatile String currentPhase = "created";
    private volatile long currentPhaseStarted;
    private volatile String boundaryOutcome = "NO_TIMEOUT";
    private String runId;
    private AtomicFile progressFile, resultFile, stallFile;
    private final ReentrantLock progressLock = new ReentrantLock();
    private final AtomicBoolean terminationClaim = new AtomicBoolean();
    private String lastWrittenPhase;
    private int lastWrittenIteration = -1;
    private static final class UnknownWork extends Error {
        UnknownWork(String reason) { super(reason); }
    }
    private void writeAtomic(AtomicFile target, JSONObject value) throws Exception {
        byte[] bytes = (value.toString() + "\n").getBytes("UTF-8");
        if (bytes.length > 131072) throw new IllegalStateException("bounded diagnostic JSON");
        FileOutputStream stream = null;
        try {
            stream = target.startWrite(); stream.write(bytes); target.finishWrite(stream);
        } catch (Throwable error) {
            if (stream != null) target.failWrite(stream);
            throw error;
        }
    }
    private JSONObject state(String status, String reason) throws Exception {
        JSONObject value = new JSONObject();
        value.put("schema_version", 1); value.put("suite", "android-pressure-r8-stability-v1");
        value.put("diagnostic_revision", 4); value.put("run_id", runId);
        value.put("status", status); value.put("phase", currentPhase);
        value.put("iteration", completed); value.put("checks", checks);
        value.put("elapsed_ms", (System.nanoTime() - started) / 1000000L);
        value.put("cleanup_safe", !unsafeCleanup);
        value.put("unsafe_cleanup", unsafeCleanup);
        value.put("inject_main_running_timeout", injectMainRunningTimeout);
        value.put("inject_progress_lock_timeout", injectProgressLockTimeout);
        value.put("foreground_requested", foregroundRequested);
        value.put("foreground_prestarted", foregroundPrestarted);
        value.put("foreground_resumed", foregroundResumed);
        value.put("foreground_finished", foregroundFinished);
        value.put("foreground_currently_resumed", foregroundActivity != null && foregroundActivity.isResumedForDiagnostic());
        value.put("fire_and_forget_state", fireAndForgetState);
        value.put("boundary_outcome", boundaryOutcome);
        value.put("reason", reason == null ? JSONObject.NULL : reason);
        return value;
    }
    private void phase(String name) {
        currentPhase = name;
        // Publish in memory BEFORE any lock acquisition or fsync. A watchdog
        // need not wait for progressLock to identify the blocked boundary.
        currentPhaseStarted = System.nanoTime();
        progressLock.lock();
        try {
            if (name.equals(lastWrittenPhase) && completed == lastWrittenIteration) return;
            try { writeAtomic(progressFile, state("RUNNING", null)); }
            catch (Exception error) { throw new AssertionError("own cache progress unavailable", error); }
            lastWrittenPhase = name; lastWrittenIteration = completed;
        } finally { progressLock.unlock(); }
    }
    private static int threadPriority(Thread thread) {
        String name = thread.getName();
        if ("main".equals(name)) return 0;
        if (name.contains("Instrumentation")) return 1;
        if (name.startsWith("memory-r8-")) return 2;
        if (name.startsWith("Thread-")) return 3;
        return 4;
    }
    private static String limited(String text, int maximum) {
        return text.length() <= maximum ? text : text.substring(0, maximum);
    }
    private JSONObject stackSnapshot(JSONObject value) throws Exception {
        Map<Thread, StackTraceElement[]> traces = Thread.getAllStackTraces();
        ArrayList<Map.Entry<Thread, StackTraceElement[]>> entries = new ArrayList<>(traces.entrySet());
        entries.sort(Comparator.comparingInt(entry -> threadPriority(entry.getKey())));
        JSONArray threads = new JSONArray();
        for (Map.Entry<Thread, StackTraceElement[]> entry : entries) {
            if (threads.length() >= 20) break;
            Thread thread = entry.getKey();
            JSONObject item = new JSONObject();
            item.put("name", limited(thread.getName(), 160));
            item.put("id", thread.getId());
            item.put("state", thread.getState().name());
            JSONArray frames = new JSONArray();
            for (StackTraceElement frame : entry.getValue()) {
                if (frames.length() >= 16) break;
                frames.put(limited(frame.toString(), 240));
            }
            item.put("frames", frames); threads.put(item);
        }
        value.put("stack_status", "CAPTURED_JAVA_THREADS_ONLY");
        value.put("observed_threads", traces.size()); value.put("threads", threads);
        value.put("stack_limits", "FIRST_20_PRIORITY_THREADS_FIRST_16_FRAMES_240_CHARS");
        return value;
    }
    /** Unknown concurrently executing work must never race ordinary finally
     * cleanup. Persist evidence, then isolate only this diagnostic process. */
    private void failStop(String reason) {
        if (!terminationClaim.compareAndSet(false, true)) {
            // Give the winning reporter a bounded opportunity to persist its
            // record; never wait on the progress monitor or file operations.
            long stopBy = System.nanoTime() + 2500000000L;
            while (System.nanoTime() < stopBy) {
                try { Thread.sleep(25); }
                catch (InterruptedException ignored) { /* Continue the bounded handoff. */ }
            }
            android.os.Process.killProcess(android.os.Process.myPid());
            throw new UnknownWork(reason);
        }
        unsafeCleanup = true;
        boundaryOutcome = "UNKNOWN_RUNNING_WORK";
        // No fatal handler waits for progressLock or file I/O: both can be the
        // blocked operation. The bounded reporter has its own AtomicFile.
        try {
            Thread reporter = new Thread(() -> {
                try {
                    JSONObject value = state("UNKNOWN", reason);
                    value.put("exit_scope", "OWN_DIAGNOSTIC_PROCESS_FAIL_STOP_NO_CONCURRENT_FREE");
                    value.put("native_resource_state", "UNKNOWN_NOT_FORCED_FREE");
                    value.put("stack_status", captureStackOnHalt ? "STACK_CAPTURE_REQUESTED" : "NOT_REQUESTED");
                    writeAtomic(stallFile, value);
                    if (captureStackOnHalt) {
                        try { stackSnapshot(value); }
                        catch (Throwable error) { value.put("stack_status", "UNAVAILABLE_" + error.getClass().getSimpleName()); }
                        writeAtomic(stallFile, value);
                    }
                    if (progressLock.tryLock()) {
                        try { writeAtomic(progressFile, value); writeAtomic(resultFile, value); }
                        finally { progressLock.unlock(); }
                    }
                } catch (Throwable ignored) { /* External observer retains the last atomic progress. */ }
            }, "memory-r8-stall-reporter");
            reporter.setDaemon(true); reporter.start(); reporter.join(2000);
        } catch (Throwable ignored) { /* Never wait indefinitely for diagnostics. */ }
        android.os.Process.killProcess(android.os.Process.myPid());
        throw new UnknownWork(reason); // Do not enter ordinary cleanup if kill returns.
    }
    private void startDeadlineWatchdog() {
        phase("watchdog.start");
        Thread watcher = new Thread(() -> {
            while (!finished) {
                long now = System.nanoTime();
                long phaseStart = currentPhaseStarted;
                if (stallTimeoutMillis > 0 && phaseStart > 0 &&
                        now - phaseStart >= stallTimeoutMillis * 1000000L && currentPhaseStarted == phaseStart) {
                    if (!finished) failStop("STALL_PHASE_TIMEOUT");
                    return;
                }
                long remaining = deadline - now;
                if (remaining <= 0) {
                    if (!finished) failStop("OVERALL_WALL_CAP_UNKNOWN_ACTIVE_BOUNDARY");
                    return;
                }
                try { Thread.sleep(Math.min(250L, Math.max(1L, remaining / 1000000L))); }
                catch (InterruptedException ignored) { return; }
            }
        }, "memory-r8-wall-watchdog");
        watcher.setDaemon(true); watcher.start();
    }
    private boolean option(String name) {
        String value = arguments.getString(name);
        if (value == null || "false".equals(value)) return false;
        if ("true".equals(value)) return true;
        throw new IllegalArgumentException(name + " requires true/false");
    }
    private void startForegroundActivity() throws Exception {
        if (!foregroundRequested) return;
        if (foregroundPrestarted) {
            phase("foreground.prestarted.lookup");
            PressureR8ForegroundActivity current = PressureR8ForegroundActivity.currentForDiagnostic();
            // The caller must have already started and resumed this exact
            // in-process Activity. Never launch a replacement on failure.
            if (current == null || !current.isResumedForDiagnostic())
                failStop("PRESTARTED_FOREGROUND_ACTIVITY_NOT_RESUMED");
            foregroundActivity = current;
            foregroundInitialPauseCount = current.pauseCountForDiagnostic();
            foregroundResumed = true;
            phase("foreground.prestarted.resumed");
            return;
        }
        AtomicReference<Activity> activity = new AtomicReference<>();
        AtomicReference<Throwable> failure = new AtomicReference<>();
        CountDownLatch returned = new CountDownLatch(1);
        Thread launch = new Thread(() -> {
            try {
                Intent intent = new Intent(getTargetContext(), PressureR8ForegroundActivity.class);
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                activity.set(startActivitySync(intent));
            } catch (Throwable error) { failure.set(error); }
            finally { returned.countDown(); }
        }, "memory-r8-foreground-launch");
        phase("foreground.launch");
        launch.start();
        if (!returned.await(10000, TimeUnit.MILLISECONDS)) failStop("FOREGROUND_LAUNCH_RUNNING_TIMEOUT");
        if (failure.get() != null || !(activity.get() instanceof PressureR8ForegroundActivity))
            failStop("FOREGROUND_LAUNCH_UNAVAILABLE");
        foregroundActivity = (PressureR8ForegroundActivity)activity.get();
        phase("foreground.await-resume");
        if (!foregroundActivity.awaitFirstResume(3000) || !foregroundActivity.isResumedForDiagnostic())
            failStop("FOREGROUND_RESUME_UNOBSERVED");
        foregroundInitialPauseCount = foregroundActivity.pauseCountForDiagnostic();
        foregroundResumed = true;
        phase("foreground.resumed");
    }
    private void checkForeground() {
        if (!foregroundRequested) return;
        ++foregroundChecks;
        PressureR8ForegroundActivity current = foregroundActivity;
        if (!foregroundResumed || current == null || !current.isResumedForDiagnostic() || current.pauseCountForDiagnostic() != foregroundInitialPauseCount) {
            boundaryOutcome = "FOREGROUND_PAUSED_OR_UNAVAILABLE";
            throw new IllegalStateException("foreground Activity not continuously resumed");
        }
    }
    /** Explicitly observe this Activity's own onStop/onDestroy before success. */
    private void finishForegroundActivity() throws Exception {
        PressureR8ForegroundActivity current = foregroundActivity;
        if (current == null || foregroundFinished) return;
        Handler main = new Handler(Looper.getMainLooper());
        CountDownLatch returned = new CountDownLatch(1);
        AtomicReference<Throwable> error = new AtomicReference<>();
        AtomicInteger running = new AtomicInteger();
        Runnable finishTask = () -> {
            if (!running.compareAndSet(0, 1)) { returned.countDown(); return; }
            try {
                // The Activity may have stopped and resumed before admission.
                // Observe only the stop caused after this specific finish.
                if (!current.isResumedForDiagnostic() || current.pauseCountForDiagnostic() != foregroundInitialPauseCount)
                    throw new IllegalStateException("foreground changed before finish");
                current.prepareStopObservation();
                current.finish();
            } catch (Throwable failure) { error.set(failure); }
            finally { running.set(2); returned.countDown(); }
        };
        phase("foreground.finish.post");
        if (!main.post(finishTask)) failStop("FOREGROUND_FINISH_POST_REFUSED");
        if (!returned.await(3000, TimeUnit.MILLISECONDS)) {
            if (running.compareAndSet(0, 3)) main.removeCallbacks(finishTask);
            if (running.get() != 2) failStop("FOREGROUND_FINISH_TASK_UNCONFIRMED");
        }
        if (error.get() != null) failStop("FOREGROUND_FINISH_FAILED");
        phase("foreground.await-stop");
        if (!current.awaitStoppedOrDestroyed(5000)) failStop("FOREGROUND_STOP_UNOBSERVED");
        foregroundFinished = true;
        phase("foreground.stopped");
    }
    private void verifyFireAndForget() throws Exception {
        phase("fire-forget.reflect");
        Field tokenField = NativePressureBackend.class.getDeclaredField("registration");
        Method exists = NativePressureBackend.class.getDeclaredMethod("nativeExists", long.class);
        tokenField.setAccessible(true); exists.setAccessible(true);
        long startedAt = System.nanoTime();
        NativePressureBackend backend = null;
        try {
            phase("fire-forget.create"); backend = NativePressureBackend.createArena(4096, 2048, 0, 256);
            long token = tokenField.getLong(backend);
            if (!((Boolean)exists.invoke(null, token))) throw new IllegalStateException("new registration absent before close");
            phase("fire-forget.close"); backend.close();
            phase("fire-forget.observe-registration");
            long until = Math.min(deadline, System.nanoTime() + 5000000000L);
            while (System.nanoTime() < until) {
                ++fireAndForgetPolls;
                if (!((Boolean)exists.invoke(null, token))) {
                    fireAndForgetState = "PASS_REGISTRATION_REMOVED_WITHOUT_CALLER_POLL";
                    fireAndForgetElapsedMillis = (System.nanoTime() - startedAt) / 1000000L;
                    return;
                }
                Thread.sleep(5);
            }
            failStop("FIRE_FORGET_CLOSE_TIMEOUT");
        } catch (UnknownWork error) { throw error; }
        catch (Throwable error) {
            // A created backend with an unobserved close outcome must not be
            // reported as safely closed or forcibly reclaimed.
            if (backend != null) failStop("FIRE_FORGET_CLOSE_UNKNOWN_" + error.getClass().getSimpleName());
            throw new IllegalStateException("fire-and-forget setup failed", error);
        }
    }
    private void check(boolean value, String reason) {
        ++checks;
        if (!value) throw new AssertionError(reason);
    }
    @Override public void onCreate(Bundle args) {
        super.onCreate(args); arguments = args == null ? new Bundle() : args; start();
    }
    private long bounded(String name, long fallback, long minimum, long maximum) {
        String text = arguments.getString(name);
        long value = text == null ? fallback : Long.parseLong(text);
        if (value < minimum || value > maximum) throw new IllegalArgumentException(name);
        return value;
    }
    private void beforeDeadline() {
        if (System.nanoTime() >= deadline) {
            boundaryOutcome = "WALL_REACHED_AT_SAFE_CHECKPOINT";
            throw new AssertionError("bounded run deadline");
        }
    }
    private long[] available(NativePressureBackend backend) throws InterruptedException {
        phase("native.snapshot");
        long until = Math.min(deadline, System.nanoTime() + 3_000_000_000L);
        do {
            long[] values = backend.snapshot();
            if (values != null) return values;
            Thread.sleep(2);
        } while (System.nanoTime() < until);
        throw new AssertionError("snapshot unavailable");
    }
    private void pressure(MemoryPressureAdapter adapter, int level) throws InterruptedException {
        phase("native.pressure." + level);
        long until = Math.min(deadline, System.nanoTime() + 3_000_000_000L);
        int status;
        do {
            status = level == MemoryPressureAdapter.NORMAL ? adapter.restoreBudget() : adapter.requestPressure(level);
            if (status == 0) return;
            if (status != 6) throw new AssertionError("pressure status " + status);
            Thread.sleep(2);
        } while (System.nanoTime() < until);
        throw new AssertionError("pressure admission busy");
    }
    private JSONObject observation() throws Exception {
        phase("observation.capture");
        ProcessMemoryObservation value = ProcessMemoryObservation.capture();
        check(value.rssKb >= 0 || value.rssUnavailableReason != null, "RSS missing reason");
        check(value.pssKb >= 0 || value.androidMemoryUnavailableReason != null, "PSS missing reason");
        JSONObject sample = new JSONObject();
        sample.put("elapsed_realtime_nanos", value.elapsedRealtimeNanos);
        sample.put("rss_kb", value.rssKb >= 0 ? value.rssKb : JSONObject.NULL);
        sample.put("pss_kb", value.pssKb >= 0 ? value.pssKb : JSONObject.NULL);
        sample.put("private_dirty_kb", value.privateDirtyKb >= 0 ? value.privateDirtyKb : JSONObject.NULL);
        sample.put("rss_unavailable_reason", value.rssUnavailableReason == null ? JSONObject.NULL : value.rssUnavailableReason);
        sample.put("android_memory_unavailable_reason", value.androidMemoryUnavailableReason == null ? JSONObject.NULL : value.androidMemoryUnavailableReason);
        return sample;
    }
    private void cycle(int index) throws Exception {
        beforeDeadline();
        phase("cycle.create-arena");
        NativePressureBackend backend = NativePressureBackend.createArena(4096, 2048, 0, 256);
        MemoryPressureAdapter adapter = null;
        NativePressureBackend.LeaseProbe lease = null;
        try {
            phase("cycle.register-callback");
            adapter = new MemoryPressureAdapter(getTargetContext(), backend);
            long size = 256L * (1 + index % 6);
            phase("cycle.hold-probe"); lease = backend.holdRetiringLeaseForDiagnostics(size);
            if (injectProgressLockTimeout && index == 0) {
                CountDownLatch held = new CountDownLatch(1);
                Thread holder = new Thread(() -> {
                    progressLock.lock();
                    try { held.countDown(); new CountDownLatch(1).await(); }
                    catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
                    finally { progressLock.unlock(); }
                }, "memory-r8-progress-lock-holder");
                holder.setDaemon(true); holder.start();
                if (!held.await(3000, TimeUnit.MILLISECONDS)) failStop("INJECTED_LOCK_HOLDER_UNKNOWN");
                phase("diagnostic.inject-progress-lock.blocked");
                throw new AssertionError("injected progress lock unexpectedly released");
            }
            long[] initial = available(backend);
            check(initial.length == 16 && initial[3] == size && initial[6] == size, "single retiring backing");
            peakBacking = Math.max(peakBacking, initial[3]);
            pressure(adapter, MemoryPressureAdapter.MODERATE);
            check(available(backend)[2] == 2048, "moderate soft budget");
            pressure(adapter, MemoryPressureAdapter.CRITICAL);
            long[] pressured = available(backend);
            long until = Math.min(deadline, System.nanoTime() + 3_000_000_000L);
            while (pressured[12] == 0 && System.nanoTime() < until) {
                Thread.sleep(2); pressured = available(backend);
            }
            check(pressured[2] == 0 && pressured[3] == size && pressured[6] == size && pressured[8] == size, "pressure keeps admitted region");
            check(pressured[12] > 0, "worker defers BUSY while lease retained");
            boolean denied = false;
            phase("cycle.probe-budget-denial");
            try (NativePressureBackend.LeaseProbe unexpected = backend.holdRetiringLeaseForDiagnostics(256)) {
                check(!unexpected.readable(), "unexpected admission");
            } catch (IllegalStateException expected) { denied = true; }
            check(denied, "zero budget rejects new admission");
            phase("cycle.read-probe"); check(lease.readable(), "pressure preserves readable lease");
            pressure(adapter, MemoryPressureAdapter.NORMAL);
            check(available(backend)[2] == 4096, "explicit restore");

            final MemoryPressureAdapter closing = adapter;
            AtomicReference<Throwable> mainError = new AtomicReference<>();
            AtomicInteger mainState = new AtomicInteger(0); // pending/running/done/cancelled
            CountDownLatch mainDone = new CountDownLatch(1);
            Handler main = new Handler(Looper.getMainLooper());
            Runnable task = () -> {
                if (!mainState.compareAndSet(0, 1)) { mainDone.countDown(); return; }
                try {
                    if (injectMainRunningTimeout && index == 0) {
                        // Diagnostic-only expected failure: no other task can
                        // open this latch. The parent must persist UNKNOWN and
                        // kill this own process without freeing these objects.
                        phase("main.injected-running-latch");
                        new CountDownLatch(1).await();
                        throw new AssertionError("injected latch unexpectedly released");
                    }
                    phase("main.close"); closing.close();
                    phase("main.poll");
                    if (closing.awaitClosed(0)) throw new AssertionError("main poll forced live lease");
                    boolean rejected = false;
                    try { closing.awaitClosed(1); } catch (IllegalStateException expected) { rejected = true; }
                    if (!rejected) throw new AssertionError("main positive wait allowed");
                } catch (Throwable error) { mainError.set(error); }
                finally { mainState.set(2); mainDone.countDown(); }
            };
            phase("main.post");
            if (!main.post(task)) { mainState.set(3); throw new AssertionError("main post refused before start"); }
            if (!mainDone.await(3000, TimeUnit.MILLISECONDS)) {
                if (mainState.compareAndSet(0, 3)) {
                    boundaryOutcome = "MAIN_TASK_CANCELLED_BEFORE_START";
                    main.removeCallbacks(task);
                    phase("main.timeout-cancelled-before-start");
                    throw new AssertionError("main post deadline; cancelled before execution");
                }
                if (mainState.get() != 2) failStop("MAIN_TASK_RUNNING_TIMEOUT");
            }
            phase("cycle.main-returned");
            check(mainError.get() == null, "main close/poll and positive-wait guard");
            phase("cycle.read-after-close");
            check(adapter.isClosed() && lease.readable(), "parent close preserves lease");
            phase("cycle.await-held-lease");
            check(!adapter.awaitClosed(1), "worker timeout never forces reclaim");
            check(adapter.requestPressure(MemoryPressureAdapter.NORMAL) == 7, "closed callback admission rejected");
            final NativePressureBackend.LeaseProbe finishing = lease;
            AtomicReference<Throwable> releaseError = new AtomicReference<>();
            Thread release = new Thread(() -> {
                try {
                    if (!finishing.readable()) throw new AssertionError("async final read");
                    finishing.close();
                } catch (Throwable error) { releaseError.set(error); }
            }, "memory-r8-last-lease");
            phase("cycle.start-last-release"); release.start();
            phase("cycle.join-last-release"); release.join(5000);
            if (release.isAlive()) failStop("LAST_RELEASE_TASK_RUNNING_TIMEOUT");
            check(!release.isAlive() && releaseError.get() == null, "async last lease completes");
            lease = null;
            phase("cycle.await-native-close");
            check(adapter.awaitClosed(5000), "native completion callback retained by R8");
            phase("cycle.final-snapshot");
            long[] end = backend.snapshot();
            check(end != null && end[0] == 2 && end[1] == 2 && end[3] == 0 && end[4] == 0 && end[5] == 0 && end[6] == 0 && end[15] == 1, "owned domain/controller closed and zero");
            phase("cycle.idempotent-close.enter"); adapter.close();
            phase("cycle.idempotent-close.returned");
            phase("cycle.idempotent-poll.enter");
            check(adapter.awaitClosed(0), "idempotent close complete");
            phase("cycle.idempotent-poll.returned");
            ++completed;
        } finally {
            if (!unsafeCleanup) {
                phase("cycle.finally-cleanup");
                if (lease != null) lease.close();
                if (adapter != null) { adapter.close(); if (!adapter.awaitClosed(5000)) failStop("CYCLE_CLEANUP_UNCONFIRMED"); }
                else { backend.close(); if (!backend.awaitClosed(5000)) failStop("CYCLE_SETUP_CLEANUP_UNCONFIRMED"); }
            }
        }
    }
    private String observeAms(String process) throws Exception {
        AtomicReference<String> output = new AtomicReference<>(), failure = new AtomicReference<>();
        Thread command = new Thread(() -> {
            try {
                phase("ams.get-ui-automation");
                android.app.UiAutomation automation = getUiAutomation();
                phase("ams.execute-shell");
                ParcelFileDescriptor descriptor = automation.executeShellCommand("am send-trim-memory " + process + " BACKGROUND");
                phase("ams.read-command");
                try (InputStream input = new ParcelFileDescriptor.AutoCloseInputStream(descriptor);
                     ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
                    byte[] buffer = new byte[512]; int count;
                    while ((count = input.read(buffer)) != -1) {
                        if (bytes.size() + count > 8192) throw new IllegalStateException("bounded shell output");
                        bytes.write(buffer, 0, count);
                    }
                    output.set(bytes.toString("UTF-8"));
                }
            } catch (Throwable error) { failure.set(error.getClass().getSimpleName()); }
        }, "memory-r8-ams-command");
        phase("ams.start-worker"); command.start(); command.join(3000);
        if (command.isAlive()) failStop("AMS_ENTRY_OR_COMMAND_RUNNING_TIMEOUT");
        phase("ams.worker-returned");
        if (failure.get() != null) return "COMMAND_UNAVAILABLE_" + failure.get();
        String text = output.get() == null ? "" : output.get();
        return text.contains("Error") || text.contains("Unknown") || text.contains("Exception") ? "AMS_REJECTED" : "COMMAND_ACCEPTED";
    }
    private JSONObject framework(boolean observeAms) throws Exception {
        phase("framework.create-arena");
        NativePressureBackend backend = NativePressureBackend.createArena(4096, 2048, 0, 256);
        MemoryPressureAdapter adapter = null;
        try {
            phase("framework.register-callback");
            adapter = new MemoryPressureAdapter(getTargetContext(), backend);
            Application application = (Application)getTargetContext().getApplicationContext();
            long ignored = adapter.ignoredLifecycleEvents();
            phase("framework.synthetic-ui-hidden"); application.onTrimMemory(20);
            check(adapter.ignoredLifecycleEvents() == ignored + 1, "UI_HIDDEN remains lifecycle only");
            long forwarded = adapter.forwardedEvents();
            phase("framework.synthetic-background"); application.onTrimMemory(40);
            check(adapter.forwardedEvents() > forwarded && available(backend)[2] == 2048, "registered BACKGROUND callback synthetic dispatch");
            pressure(adapter, MemoryPressureAdapter.NORMAL);
            JSONObject result = new JSONObject();
            result.put("application_dispatch", "PASS_SYNTHETIC_BACKGROUND_AND_UI_HIDDEN");
            result.put("legacy_automatic_delivery", Build.VERSION.SDK_INT >= 34 ? "NOT_EXPECTED_SINCE_API34_PER_ANDROID_CONTRACT" : "NOT_OBSERVED_BY_THIS_TEST");
            result.put("onLowMemory_automatic_delivery", Build.VERSION.SDK_INT >= 34 ? "NEVER_CALLED_SINCE_API34_PER_ANDROID_CONTRACT" : "NOT_OBSERVED_BY_THIS_TEST");
            result.put("observe_ams", observeAms);
            if (!observeAms) result.put("ams_background_injection", "NOT_REQUESTED_SEPARATE_SMOKE_EVIDENCE");
            else {
                String process = getTargetContext().getPackageName();
                if (!process.matches("[A-Za-z0-9_.]+")) throw new AssertionError("diagnostic package");
                long before = adapter.forwardedEvents();
                String outcome = observeAms(process);
                if ("COMMAND_ACCEPTED".equals(outcome)) {
                    long until = Math.min(deadline, System.nanoTime() + 3_000_000_000L);
                    while (adapter.forwardedEvents() == before && System.nanoTime() < until) Thread.sleep(5);
                    outcome = adapter.forwardedEvents() > before ? "DELIVERED" : "COMMAND_ACCEPTED_NO_CALLBACK_OBSERVED";
                    if (adapter.forwardedEvents() > before) check(available(backend)[2] == 2048, "injected callback applies budget");
                }
                result.put("ams_background_injection", outcome);
            }
            result.put("automatic_low_ram_tested", false);
            result.put("injection_scope", "OWN_DIAGNOSTIC_PACKAGE_ONLY");
            return result;
        } finally {
            if (!unsafeCleanup) {
                phase("framework.close");
                if (adapter != null) { adapter.close(); boolean closed = adapter.awaitClosed(5000); if (!closed) failStop("FRAMEWORK_CLEANUP_UNCONFIRMED"); check(closed, "framework test clean close"); }
                else { backend.close(); boolean closed = backend.awaitClosed(5000); if (!closed) failStop("FRAMEWORK_SETUP_CLEANUP_UNCONFIRMED"); check(closed, "framework setup cleanup"); }
            }
        }
    }
    @Override public void onStart() {
        Bundle results = new Bundle();
        started = System.nanoTime(); runId = Long.toHexString(started);
        try {
            File cache = getTargetContext().getCacheDir();
            progressFile = new AtomicFile(new File(cache, "mgmem-r8-progress.json"));
            resultFile = new AtomicFile(new File(cache, "mgmem-r8-result.json"));
            stallFile = new AtomicFile(new File(cache, "mgmem-r8-stall.json"));
            phase("arguments.validate");
            int iterations = (int)bounded("iterations", 100, 1, 100000);
            long minimum = bounded("min_duration_ms", 0, 0, 1800000);
            long maximum = bounded("max_duration_ms", 180000, 1000, 3600000);
            int cadence = (int)bounded("sample_every", 100, 1, 100000);
            boolean observeAms = option("observe_ams");
            foregroundRequested = option("foreground");
            foregroundPrestarted = option("foreground_prestarted");
            if (foregroundPrestarted && !foregroundRequested) throw new IllegalArgumentException("foreground_prestarted requires foreground");
            injectMainRunningTimeout = option("inject_main_running_timeout");
            injectProgressLockTimeout = option("inject_progress_lock_timeout");
            captureStackOnHalt = option("capture_stack_on_halt");
            stallTimeoutMillis = bounded("stall_timeout_ms", 0, 0, 60000);
            if (stallTimeoutMillis > 0 && stallTimeoutMillis < 1000) throw new IllegalArgumentException("stall timeout minimum");
            if (injectProgressLockTimeout && stallTimeoutMillis == 0) throw new IllegalArgumentException("injected lock needs stall watchdog");
            if (minimum >= maximum) throw new IllegalArgumentException("duration bounds");
            deadline = started + maximum * 1000000L;
            startDeadlineWatchdog();
            check(Looper.myLooper() != Looper.getMainLooper(), "instrumentation worker required");
            startForegroundActivity();
            checkForeground();
            String marker = new R8ConsumerMarker().getClass().getName();
            // R8 may rewrite a class-name string literal together with a class
            // reference. The external validator compares this reported name
            // with the exact mapping from this APK's build receipt instead.
            check(marker != null && !marker.isEmpty(), "R8 runtime marker available");
            JSONObject callback = framework(observeAms);
            verifyFireAndForget();
            JSONArray observations = new JSONArray(); observations.put(observation());
            while (completed < iterations || (System.nanoTime() - started) / 1000000L < minimum) {
                if (completed >= 100000) {
                    boundaryOutcome = "CYCLE_CAP_REACHED_AT_SAFE_CHECKPOINT";
                    throw new AssertionError("cycle cap");
                }
                checkForeground(); cycle(completed); checkForeground();
                if (completed % cadence == 0 && observations.length() < 256) observations.put(observation());
            }
            observations.put(observation());
            checkForeground();
            foregroundPauseCountBeforeFinish = foregroundActivity == null ? 0 : foregroundActivity.pauseCountForDiagnostic() - foregroundInitialPauseCount;
            finishForegroundActivity();
            beforeDeadline();
            JSONObject report = new JSONObject();
            report.put("suite", "android-pressure-r8-stability-v1"); report.put("status", "PASS");
            report.put("checks", checks); report.put("iterations_requested", iterations); report.put("iterations_completed", completed);
            report.put("minimum_duration_ms", minimum); report.put("maximum_duration_ms", maximum);
            report.put("actual_duration_ms", (System.nanoTime() - started) / 1000000L);
            report.put("api", Build.VERSION.SDK_INT); report.put("r8_marker_runtime_name", marker);
            report.put("diagnostic_revision", 4); report.put("run_id", runId);
            report.put("inject_main_running_timeout", injectMainRunningTimeout);
            report.put("inject_progress_lock_timeout", injectProgressLockTimeout);
            report.put("stall_timeout_ms", stallTimeoutMillis);
            report.put("foreground_requested", foregroundRequested);
            report.put("foreground_prestarted", foregroundPrestarted);
            report.put("foreground_resumed", foregroundResumed);
            report.put("foreground_finished", foregroundFinished);
            report.put("foreground_resume_checks", foregroundChecks);
            report.put("foreground_pause_count_before_finish", foregroundPauseCountBeforeFinish);
            report.put("foreground_preexisting_pause_count", foregroundInitialPauseCount);
            report.put("process_pid", android.os.Process.myPid());
            report.put("foreground_finish_policy", "OWN_MAIN_FINISH_THEN_OBSERVED_ONSTOP_OR_ONDESTROY");
            report.put("fire_and_forget_state", fireAndForgetState);
            report.put("fire_and_forget_elapsed_ms", fireAndForgetElapsedMillis);
            report.put("fire_and_forget_polls", fireAndForgetPolls);
            report.put("fire_and_forget_scope", "CALLER_ONLY_CLOSE_AND_NATIVE_EXISTS_NO_AWAIT_SNAPSHOT_OR_NATIVE_POLL");
            report.put("phase", "complete"); report.put("exit_scope", "NORMAL_INSTRUMENTATION_COMPLETION");
            report.put("progress_file", "cache/mgmem-r8-progress.json");
            report.put("unknown_work_policy", "PERSIST_UNKNOWN_AND_FAIL_STOP_OWN_PROCESS_NO_CONCURRENT_FREE");
            report.put("framework", callback); report.put("own_process_observations", observations);
            report.put("observation_scope", "WHOLE_OWN_PROCESS_NOT_DOMAIN_ATTRIBUTION_NOT_ATOMIC_NOT_SAVINGS");
            report.put("peak_requested_backing_bytes_per_domain", peakBacking); report.put("final_backing_bytes_each_domain", 0);
            report.put("stability_scope", "BOUNDED_CREATE_PRESSURE_RESTORE_MAIN_CLOSE_ASYNC_LAST_RELEASE_CYCLES");
            report.put("indefinite_leak_freedom_proven", false); report.put("actual_low_memory_event", false);
            report.put("performance_qualified", false); report.put("BIL_integrated", false);
            report.put("energy_joules", JSONObject.NULL); report.put("energy_unavailable_reason", "NO_QUALIFIED_SAME_WORK_ENERGY_METHOD");
            progressLock.lock();
            try {
                currentPhase = "complete";
                writeAtomic(progressFile, state("PASS", null)); writeAtomic(resultFile, report);
            } finally { progressLock.unlock(); }
            finished = true;
            results.putString("report_json", report.toString()); finish(0, results);
        } catch (Throwable error) {
            if (unsafeCleanup) failStop("UNKNOWN_WORK_NO_NORMAL_FINALLY_CLEANUP");
            try { finishForegroundActivity(); }
            catch (Throwable failure) {
                if (unsafeCleanup) failStop("FOREGROUND_FINISH_UNKNOWN");
                failStop("FOREGROUND_FINISH_UNCONFIRMED");
            }
            JSONObject report;
            try {
                report = state("FAIL", error.getClass().getSimpleName());
                report.put("iterations_completed", completed);
                report.put("exit_scope", "NORMAL_FAILURE_AFTER_SAFE_CLEANUP");
                progressLock.lock();
                try {
                    if (progressFile != null) writeAtomic(progressFile, report);
                    if (resultFile != null) writeAtomic(resultFile, report);
                } finally { progressLock.unlock(); }
            } catch (Exception unavailable) { report = new JSONObject(); }
            finished = true;
            results.putString("report_json", report.toString());
            results.putString("failure", error.toString()); finish(1, results);
        }
    }
}

// A deliberately retained, renameable consumer witness. It does not keep the
// adapter package, so production adapter shrink/optimization remains enabled.
final class R8ConsumerMarker { R8ConsumerMarker() {} }
final class R8UnusedMarker { static String neverCalled() { return "removed-by-real-r8"; } }
