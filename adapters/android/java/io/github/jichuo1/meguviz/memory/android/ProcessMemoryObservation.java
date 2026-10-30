package io.github.jichuo1.meguviz.memory.android;

import android.os.Debug;
import android.os.SystemClock;
import java.io.BufferedReader;
import java.io.FileReader;
import java.io.IOException;

/** Own-process observations, in Android kB units, separately from domain bytes.
 * No other process, application payload, absolute path or energy estimate is read. */
public final class ProcessMemoryObservation {
    public final long elapsedRealtimeNanos, rssKb, pssKb, privateDirtyKb;
    public final String rssUnavailableReason, androidMemoryUnavailableReason;
    public final Double energyJoules = null;
    public final String energyUnavailableReason = "NO_QUALIFIED_SAME_WORK_ENERGY_METHOD";
    private ProcessMemoryObservation(long clock, long rss, long pss, long dirty, String rssReason, String androidReason) {
        elapsedRealtimeNanos = clock; rssKb = rss; pssKb = pss; privateDirtyKb = dirty;
        rssUnavailableReason = rssReason; androidMemoryUnavailableReason = androidReason;
    }
    public static ProcessMemoryObservation capture() {
        long rss = -1, pss = -1, dirty = -1;
        String rssReason = null, androidReason = null;
        try (BufferedReader reader = new BufferedReader(new FileReader("/proc/self/status"))) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (!line.startsWith("VmRSS:")) continue;
                if (rss >= 0) throw new IOException("duplicate RSS");
                String[] values = line.substring(6).trim().split("\\s+");
                if (values.length != 2 || !"kB".equals(values[1])) throw new IOException("unknown RSS units");
                rss = Long.parseLong(values[0]);
                if (rss <= 0) throw new IOException("unusable RSS for a running process");
            }
            if (rss < 0) rssReason = "OWN_PROCESS_VMRSS_UNAVAILABLE";
        } catch (IOException | SecurityException | NumberFormatException error) {
            rss = -1; rssReason = "OWN_PROCESS_VMRSS_IO_PERMISSION_OR_FORMAT_UNAVAILABLE";
        }
        try {
            Debug.MemoryInfo info = new Debug.MemoryInfo();
            Debug.getMemoryInfo(info);
            pss = info.getTotalPss(); dirty = info.getTotalPrivateDirty();
            if (pss <= 0 || dirty < 0) { pss = -1; dirty = -1; androidReason = "ANDROID_MEMORYINFO_ZERO_OR_INVALID_TOTAL_PSS"; }
        } catch (RuntimeException | LinkageError error) {
            pss = -1; dirty = -1; androidReason = "ANDROID_MEMORYINFO_UNAVAILABLE";
        }
        return new ProcessMemoryObservation(SystemClock.elapsedRealtimeNanos(),rss,pss,dirty,rssReason,androidReason);
    }
}
