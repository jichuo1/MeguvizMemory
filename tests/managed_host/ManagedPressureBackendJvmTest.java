package io.github.jichuo1.meguviz.memory.android;

import android.content.Context;
import java.lang.reflect.Method;

/** The Android types here are host stubs, not an Android runtime test. */
public final class ManagedPressureBackendJvmTest {
    public static void main(String[] args) throws Exception {
        ManagedMemoryDomain d = ManagedMemoryDomain.system(8192);
        ManagedPressureBackend backend = new ManagedPressureBackend(d.attachPressure(4096, 1024));
        MemoryPressureAdapter adapter = new MemoryPressureAdapter(new Context(), backend);
        Method quarantine = ManagedMemoryDomain.class.getDeclaredMethod("quarantineAfterUnknown", int.class);
        quarantine.setAccessible(true);
        quarantine.invoke(d, 9);
        adapter.onTrimMemory(15);
        adapter.onLowMemory();
        if (backend.rejectedEvents() != 2 || backend.lastNotificationStatus() != 9 || adapter.forwardedEvents() != 0)
            throw new AssertionError("quarantined callback rejection was not observable");
        if (backend.awaitClosed(0)) throw new AssertionError("quarantine was reported as closed");
        try { adapter.close(); throw new AssertionError("quarantined close reported success"); }
        catch (ManagedMemoryDomain.ManagedException expected) {
            if (expected.status() != 9) throw expected;
        }
        System.out.println("managed pressure callback rejection passed (host stubs)");
    }
}
