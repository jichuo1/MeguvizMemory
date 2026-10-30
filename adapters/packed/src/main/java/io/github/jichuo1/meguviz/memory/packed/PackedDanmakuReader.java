package io.github.jichuo1.meguviz.memory.packed;

import java.nio.charset.StandardCharsets;
import java.util.Arrays;

/**
 * Primitive reader for the canonical danmaku-wire flat result.
 *
 * <p>This is an ordinary JVM consumer adapter, independent of the Memory C ABI.
 * It creates no per-element descriptor objects. copyOf creates two bounded
 * primitive snapshots. borrowImmutable keeps strong references but requires
 * callers to stop mutating both arrays until close. Neither factory promises a
 * coherent snapshot of concurrently modified input. Access and close are
 * serialized; close only drops references, not a promise of immediate GC/RSS
 * reclamation. No raw input arrays or escaping element views are exposed.</p>
 */
public final class PackedDanmakuReader implements AutoCloseable {
    public static final int MAX_REPLY_BYTES = 16 * 1024 * 1024;
    public static final int MAX_ELEMENTS = 65_536;
    public static final int MAX_SOURCES = 65_536;
    public static final int MAX_FLAT_INTS = 2 + 5 * MAX_ELEMENTS + 2 * MAX_SOURCES;
    public static final long MAX_VISIBLE_PAYLOAD_BYTES = 4L * MAX_FLAT_INTS + MAX_REPLY_BYTES;
    public static final long MAX_DECISION_PAYLOAD_BYTES = 2L * MAX_ELEMENTS + MAX_SOURCES;

    private int[] flat;
    private byte[] reply;
    private final int elements;
    private final int sources;
    private final boolean copied;
    private final long payloadBytes;

    public static final class BudgetExceededException extends IllegalArgumentException {
        private static final long serialVersionUID = 1L;
        BudgetExceededException() { super("packed payload budget exceeded"); }
    }

    @FunctionalInterface
    public interface ScanProvider { int[] scan(); }

    /**
     * Preserve existing engine admission: only unavailability selects the JVM
     * provider. A selected native rejection or invalid flat result returns null
     * without invoking the JVM provider. Both arrays then obey borrowImmutable.
     */
    public static PackedDanmakuReader selectImmutable(boolean nativeAvailable,
            ScanProvider nativeScan, ScanProvider jvmScan, byte[] reply, long maxVisiblePayloadBytes) {
        ScanProvider selected = nativeAvailable ? nativeScan : jvmScan;
        if (selected == null) throw new IllegalArgumentException("selected scan provider missing");
        int[] result = selected.scan();
        if (result == null) return null;
        try { return borrowImmutable(result, reply, maxVisiblePayloadBytes); }
        catch (IllegalArgumentException invalid) { return null; }
    }

    /** Independent bitmaps, with the same meanings as the existing wire rewriter. */
    public static final class Decision {
        public final boolean[] keepElements;
        public final boolean[] recolorElements;
        public final boolean[] keepSources;
        public final boolean changed;
        public final boolean usableWeight;
        public final long elementVisits;
        public final long sourceVisits;

        private Decision(boolean[] keep, boolean[] recolor, boolean[] keepSrc,
                         boolean changed, boolean usableWeight, long elementVisits, long sourceVisits) {
            this.keepElements = keep;
            this.recolorElements = recolor;
            this.keepSources = keepSrc;
            this.changed = changed;
            this.usableWeight = usableWeight;
            this.elementVisits = elementVisits;
            this.sourceVisits = sourceVisits;
        }
    }

    private PackedDanmakuReader(int[] flat, byte[] reply, boolean copied) {
        validate(flat, reply.length);
        this.flat = flat;
        this.reply = reply;
        this.elements = flat[0];
        this.sources = flat[1];
        this.copied = copied;
        this.payloadBytes = visibleBytes(flat.length, reply.length);
    }

    public static PackedDanmakuReader copyOf(int[] flat, byte[] reply) {
        return copyOf(flat, reply, MAX_VISIBLE_PAYLOAD_BYTES);
    }

    /** Budget is checked before either array clone; validation then uses the clones. */
    public static PackedDanmakuReader copyOf(int[] flat, byte[] reply, long maxVisiblePayloadBytes) {
        checkInputs(flat, reply, maxVisiblePayloadBytes);
        return new PackedDanmakuReader(Arrays.copyOf(flat, flat.length),
                Arrays.copyOf(reply, reply.length), true);
    }

    public static PackedDanmakuReader borrowImmutable(int[] flat, byte[] reply) {
        return borrowImmutable(flat, reply, MAX_VISIBLE_PAYLOAD_BYTES);
    }

    /** Caller owns immutability; retaining a reference alone cannot enforce it. */
    public static PackedDanmakuReader borrowImmutable(int[] flat, byte[] reply, long maxVisiblePayloadBytes) {
        checkInputs(flat, reply, maxVisiblePayloadBytes);
        return new PackedDanmakuReader(flat, reply, false);
    }

    private static long visibleBytes(int flatLength, int replyLength) {
        return 4L * flatLength + replyLength;
    }

    private static void checkInputs(int[] flat, byte[] reply, long budget) {
        if (flat == null || reply == null || budget < 0 || flat.length > MAX_FLAT_INTS ||
                reply.length > MAX_REPLY_BYTES) throw new IllegalArgumentException("packed input bounds");
        if (visibleBytes(flat.length, reply.length) > budget) throw new BudgetExceededException();
    }

    private static void validate(int[] flat, int replyLength) {
        if (flat.length < 2) throw new IllegalArgumentException("packed header missing");
        int n = flat[0], s = flat[1];
        if (n < 0 || n > MAX_ELEMENTS || s < 0 || s > MAX_SOURCES ||
                2L + 5L * n + 2L * s != flat.length)
            throw new IllegalArgumentException("packed count or exact length mismatch");
        for (int index = 0, at = 2; index < n; ++index, at += 5) {
            int flags = flat[at + 2];
            if ((flags & ~7) != 0) throw new IllegalArgumentException("unknown element presence bits");
            if ((flags & 4) != 0) {
                int start = flat[at + 3], length = flat[at + 4];
                if (start < 0 || length < 0 || start > replyLength || length > replyLength - start)
                    throw new IllegalArgumentException("packed content range outside reply");
            }
            // Absent content offsets differ between the real Rust (0) and
            // Kotlin (-1) producers. They are ignored, as in fromFlattened.
        }
        for (int index = 0, at = 2 + 5 * n; index < s; ++index, at += 2)
            if (flat[at + 1] != 0 && flat[at + 1] != 1)
                throw new IllegalArgumentException("noncanonical source presence flag");
    }

    private void open() {
        if (flat == null) throw new IllegalStateException("packed reader is closed");
    }

    private int element(int index) {
        open();
        if (index < 0 || index >= elements) throw new IndexOutOfBoundsException("element index");
        return 2 + 5 * index;
    }

    private int source(int index) {
        open();
        if (index < 0 || index >= sources) throw new IndexOutOfBoundsException("source index");
        return 2 + 5 * elements + 2 * index;
    }

    public synchronized int elementCount() { open(); return elements; }
    public synchronized int sourceCount() { open(); return sources; }
    public synchronized boolean isClosed() { return flat == null; }
    public synchronized long visiblePayloadBytes() { return flat == null ? 0 : payloadBytes; }
    public synchronized long ownedPayloadBytes() { return flat != null && copied ? payloadBytes : 0; }
    public synchronized long borrowedPayloadBytes() { return flat != null && !copied ? payloadBytes : 0; }
    public synchronized boolean hasWeight(int index) { return (flat[element(index) + 2] & 1) != 0; }
    public synchronized int weightOr(int index, int fallback) {
        int at = element(index); return (flat[at + 2] & 1) != 0 ? flat[at] : fallback;
    }
    public synchronized boolean hasColorful(int index) { return (flat[element(index) + 2] & 2) != 0; }
    public synchronized int colorfulOr(int index, int fallback) {
        int at = element(index); return (flat[at + 2] & 2) != 0 ? flat[at + 1] : fallback;
    }
    public synchronized boolean hasContent(int index) { return (flat[element(index) + 2] & 4) != 0; }
    public synchronized int contentStart(int index) {
        int at = element(index); return (flat[at + 2] & 4) != 0 ? flat[at + 3] : -1;
    }
    public synchronized int contentLength(int index) {
        int at = element(index); return (flat[at + 2] & 4) != 0 ? flat[at + 4] : 0;
    }
    /** Decode only on demand; text/normalization costs are not claimed eliminated. */
    public synchronized String contentUtf8(int index) {
        int at = element(index);
        return (flat[at + 2] & 4) == 0 ? "" :
                new String(reply, flat[at + 3], flat[at + 4], StandardCharsets.UTF_8);
    }
    public synchronized boolean hasSourceKind(int index) { return flat[source(index) + 1] != 0; }
    public synchronized int sourceKindOr(int index, int fallback) {
        int at = source(index); return flat[at + 1] != 0 ? flat[at] : fallback;
    }

    public synchronized Decision decide(boolean hasThreshold, int minimumWeight,
                                        boolean removeVipColorful, int vipValue, boolean[] semanticBlocked) {
        return decide(hasThreshold, minimumWeight, removeVipColorful, vipValue,
                semanticBlocked, MAX_DECISION_PAYLOAD_BYTES);
    }

    /**
     * semanticBlocked is supplied by the existing semantic judge/normalizer.
     * Caller must not mutate that policy bitmap during this call.
     * No native-availability fallback, host parseFrom, or logging policy is changed.
     * Decision payload budget covers the three boolean arrays, excluding headers.
     */
    public synchronized Decision decide(boolean hasThreshold, int minimumWeight,
                                        boolean removeVipColorful, int vipValue,
                                        boolean[] semanticBlocked, long maxDecisionPayloadBytes) {
        open();
        if (semanticBlocked != null && semanticBlocked.length != elements)
            throw new IllegalArgumentException("semantic bitmap length mismatch");
        if (maxDecisionPayloadBytes < 0) throw new IllegalArgumentException("negative bitmap budget");
        if (2L * elements + sources > maxDecisionPayloadBytes) throw new BudgetExceededException();
        boolean usable = !hasThreshold;
        long visits = 0;
        if (hasThreshold) {
            for (int index = 0, at = 2; index < elements; ++index, at += 5) {
                ++visits;
                if ((flat[at + 2] & 1) != 0 && flat[at] > 0) { usable = true; break; }
            }
        }
        boolean[] keep = new boolean[elements], recolor = new boolean[elements], keepSrc = new boolean[sources];
        boolean changed = false;
        for (int index = 0, at = 2; index < elements; ++index, at += 5) {
            ++visits;
            int flags = flat[at + 2];
            boolean weightKeep = !hasThreshold || !usable || (flags & 1) == 0 || flat[at] >= minimumWeight;
            keep[index] = weightKeep && (semanticBlocked == null || !semanticBlocked[index]);
            recolor[index] = removeVipColorful && keep[index] && (flags & 2) != 0 && flat[at + 1] == vipValue;
            changed |= !keep[index] || recolor[index];
        }
        for (int index = 0, at = 2 + 5 * elements; index < sources; ++index, at += 2) {
            keepSrc[index] = !removeVipColorful || flat[at + 1] == 0 || flat[at] != vipValue;
            changed |= !keepSrc[index];
        }
        return new Decision(keep, recolor, keepSrc, changed, usable, visits, sources);
    }

    /** Existing independent decisions/Strings remain valid; backing arrays are not exposed. */
    @Override public synchronized void close() { flat = null; reply = null; }
}
