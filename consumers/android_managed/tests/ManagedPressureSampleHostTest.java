package io.github.jichuo1.meguviz.memory.sample;

import android.content.ComponentCallbacks2;
import android.content.Context;
import io.github.jichuo1.meguviz.memory.android.ManagedMemoryDomain;

/** Host JVM + synthetic Android stubs only; not an Android framework test. */
public final class ManagedPressureSampleHostTest {
    private static final class ContextSpy extends Context {
        int registered, unregistered;
        ComponentCallbacks2 callback;
        @Override public void registerComponentCallbacks(ComponentCallbacks2 value) {
            if (callback != null) throw new AssertionError("duplicate registration");
            callback = value; ++registered;
        }
        @Override public void unregisterComponentCallbacks(ComponentCallbacks2 value) {
            if (callback != value) throw new AssertionError("wrong callback");
            callback = null; ++unregistered;
        }
    }
    private static final class RegisterFailureContext extends Context {
        int registered;
        @Override public void registerComponentCallbacks(ComponentCallbacks2 value) {
            ++registered;
            throw new IllegalStateException("injected registration failure");
        }
        @Override public void unregisterComponentCallbacks(ComponentCallbacks2 value) {
            throw new AssertionError("registration did not complete");
        }
    }

    public static void main(String[] args) throws Exception {
        ContextSpy context = new ContextSpy();
        ManagedPressureSample sample = new ManagedPressureSample(context);
        sample.openOnWorker();
        if (context.registered != 1 || context.callback == null) throw new AssertionError("not registered");
        ManagedPressureSample.EventObservation event = sample.submitSyntheticCriticalOnWorker();
        if (event.ticket <= 0 || event.state != ManagedMemoryDomain.Pressure.EVENT_APPLIED ||
                event.status != 0 || event.applySequence <= 0 || event.frameworkForwarded != 0 || event.frameworkRejected != 0)
            throw new AssertionError("synthetic ticket outcome invalid");
        if (!sample.closeOnWorker() || !sample.closeOnWorker()) throw new AssertionError("close not complete/idempotent");
        if (context.unregistered != 1 || context.callback != null) throw new AssertionError("not unregistered once");
        boolean rejected = false;
        try { sample.submitSyntheticCriticalOnWorker(); } catch (IllegalStateException expected) { rejected = true; }
        if (!rejected) throw new AssertionError("closed session accepted an event");
        RegisterFailureContext failingContext = new RegisterFailureContext();
        ManagedPressureSample failed = new ManagedPressureSample(failingContext);
        boolean failedOpen = false;
        try { failed.openOnWorker(); } catch (IllegalStateException expected) { failedOpen = true; }
        if (!failedOpen || failingContext.registered != 1) throw new AssertionError("failure was not reported");
        if (!failed.closeOnWorker()) throw new AssertionError("partially opened domain was not cleaned up");
        System.out.println("PASS: host JNI, synthetic event, and stub callback lifecycle; no Android runtime");
    }
}
