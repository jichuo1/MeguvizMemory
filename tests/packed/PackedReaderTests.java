import io.github.jichuo1.meguviz.memory.packed.PackedDanmakuReader;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicReference;

public final class PackedReaderTests {
    private static int cases;
    private static void check(boolean value) { if (!value) throw new AssertionError("packed contract"); }
    private static void rejects(Class<? extends Throwable> type, Runnable action) {
        try { action.run(); } catch (Throwable problem) {
            if (type.isInstance(problem)) return;
            throw new AssertionError("unexpected rejection type", problem);
        }
        throw new AssertionError("input unexpectedly accepted");
    }
    private static void invalid(int[] flat, byte[] bytes) {
        rejects(IllegalArgumentException.class, () -> PackedDanmakuReader.copyOf(flat, bytes));
        ++cases;
    }
    public static void main(String[] args) throws Exception {
        byte[] reply = "zero/hello".getBytes(StandardCharsets.UTF_8);
        int[] flat = {3, 2, 0,0,0,0,0, 0,0,7,5,5, Integer.MIN_VALUE,Integer.MAX_VALUE,3,0,0,
                      0,1, 12345,0};
        try (PackedDanmakuReader reader = PackedDanmakuReader.copyOf(flat, reply)) {
            check(reader.elementCount()==3 && reader.sourceCount()==2);
            check(!reader.hasWeight(0) && reader.weightOr(0,99)==99);
            check(reader.hasWeight(1) && reader.weightOr(1,99)==0);
            check(reader.hasColorful(1) && reader.colorfulOr(1,99)==0);
            check(reader.weightOr(2,0)==Integer.MIN_VALUE && reader.colorfulOr(2,0)==Integer.MAX_VALUE);
            check(!reader.hasContent(0) && reader.contentStart(0)==-1 && reader.contentLength(0)==0);
            check(reader.hasContent(1) && reader.contentUtf8(1).equals("hello"));
            check(reader.hasSourceKind(0) && reader.sourceKindOr(0,-1)==0);
            check(!reader.hasSourceKind(1) && reader.sourceKindOr(1,-1)==-1);
            check(reader.ownedPayloadBytes()==4L*flat.length+reply.length && reader.borrowedPayloadBytes()==0);
            flat[2]=99; flat[7]=99; reply[5]='X';
            check(reader.weightOr(0,99)==99 && reader.weightOr(1,99)==0 && reader.contentUtf8(1).equals("hello"));
            rejects(IndexOutOfBoundsException.class, () -> reader.hasWeight(-1));
            rejects(IndexOutOfBoundsException.class, () -> reader.hasWeight(3));
            rejects(IndexOutOfBoundsException.class, () -> reader.hasSourceKind(2));
            ++cases;
        }
        try (PackedDanmakuReader r=PackedDanmakuReader.borrowImmutable(new int[]{1,0,0,0,0,-1,0},new byte[0])) {
            check(r.visiblePayloadBytes()==28 && r.ownedPayloadBytes()==0 && r.borrowedPayloadBytes()==28);
            check(r.contentStart(0)==-1 && r.contentUtf8(0).isEmpty()); ++cases;
        }
        try (PackedDanmakuReader r=PackedDanmakuReader.copyOf(new int[]{1,0,0,0,4,0,0},new byte[0])) {
            check(r.hasContent(0) && r.contentStart(0)==0 && r.contentLength(0)==0); ++cases;
        }
        try (PackedDanmakuReader r=PackedDanmakuReader.copyOf(new int[]{0,0},new byte[0],8)) {
            check(r.elementCount()==0 && r.sourceCount()==0 && !r.decide(true,10,true,1,null).changed); ++cases;
        }
        invalid(new int[0],new byte[0]); invalid(new int[]{0},new byte[0]);
        invalid(new int[]{-1,0},new byte[0]); invalid(new int[]{0,-1},new byte[0]);
        invalid(new int[]{Integer.MAX_VALUE,Integer.MAX_VALUE},new byte[0]);
        invalid(new int[]{65_537,0},new byte[0]); invalid(new int[]{0,65_537},new byte[0]);
        invalid(new int[]{1,0,0,0,0},new byte[0]); invalid(new int[]{0,0,1},new byte[0]);
        invalid(new int[]{1,0,0,0,8,0,0},new byte[0]);
        invalid(new int[]{0,1,0,2},new byte[0]);
        invalid(new int[]{1,0,0,0,4,-1,0},new byte[0]);
        invalid(new int[]{1,0,0,0,4,0,-1},new byte[0]);
        invalid(new int[]{1,0,0,0,4,Integer.MAX_VALUE,1},new byte[0]);
        invalid(new int[]{1,0,0,0,4,1,Integer.MAX_VALUE},new byte[2]);
        invalid(new int[]{1,0,0,0,4,1,2},new byte[2]);
        rejects(IllegalArgumentException.class, () -> PackedDanmakuReader.copyOf(null,new byte[0])); ++cases;
        rejects(IllegalArgumentException.class, () -> PackedDanmakuReader.copyOf(new int[]{0,0},null)); ++cases;
        rejects(PackedDanmakuReader.BudgetExceededException.class,
                () -> PackedDanmakuReader.copyOf(new int[]{0,0},new byte[0],7)); ++cases;
        try (PackedDanmakuReader r=PackedDanmakuReader.copyOf(new int[]{1,0,1,2,3,0,0},new byte[0])) {
            rejects(PackedDanmakuReader.BudgetExceededException.class, () -> r.decide(false,0,false,0,null,1));
            check(r.elementCount()==1);
            rejects(IllegalArgumentException.class, () -> r.decide(false,0,false,0,new boolean[0])); ++cases;
        }
        int[] noPositive={3,0,0,0,0,0,0,-1,0,1,0,0,0,0,1,0,0};
        try (PackedDanmakuReader r=PackedDanmakuReader.copyOf(noPositive,new byte[0])) {
            PackedDanmakuReader.Decision d=r.decide(true,10,false,0,null);
            check(!d.usableWeight && Arrays.equals(d.keepElements,new boolean[]{true,true,true})); ++cases;
        }
        int[] mixed={3,2,0,0,0,0,0,1,0,1,0,0,20,60001,3,0,0,60001,1,0,0};
        PackedDanmakuReader.Decision escaped;
        try (PackedDanmakuReader r=PackedDanmakuReader.copyOf(mixed,new byte[0])) {
            escaped=r.decide(true,10,true,60001,new boolean[]{false,false,false});
            check(Arrays.equals(escaped.keepElements,new boolean[]{true,false,true}));
            check(Arrays.equals(escaped.recolorElements,new boolean[]{false,false,true}));
            check(Arrays.equals(escaped.keepSources,new boolean[]{false,true}) && escaped.changed); ++cases;
        }
        check(escaped.keepElements[2]); ++cases;
        PackedDanmakuReader closed=PackedDanmakuReader.copyOf(new int[]{0,0},new byte[0]);
        closed.close();closed.close();check(closed.isClosed() && closed.ownedPayloadBytes()==0 && closed.visiblePayloadBytes()==0);
        rejects(IllegalStateException.class, closed::elementCount);
        rejects(IllegalStateException.class, () -> closed.hasWeight(0));
        rejects(IllegalStateException.class, () -> closed.weightOr(0,0));
        rejects(IllegalStateException.class, () -> closed.decide(false,0,false,0,null)); ++cases;
        final int[] providerCalls=new int[2];
        check(PackedDanmakuReader.selectImmutable(true,()->{++providerCalls[0];return null;},
              ()->{++providerCalls[1];return new int[]{0,0};},new byte[0],8)==null);
        check(providerCalls[0]==1 && providerCalls[1]==0); ++cases;
        check(PackedDanmakuReader.selectImmutable(true,()->new int[]{1,0},
              ()->{++providerCalls[1];return new int[]{0,0};},new byte[0],100)==null);
        check(providerCalls[1]==0); ++cases;
        try (PackedDanmakuReader r=PackedDanmakuReader.selectImmutable(false,
             ()->{throw new AssertionError("native called when unavailable");},
             ()->{++providerCalls[1];return new int[]{0,0};},new byte[0],8)) {
            check(r.elementCount()==0 && providerCalls[1]==1); ++cases;
        }
        AtomicReference<Throwable> failure=new AtomicReference<>();
        CountDownLatch admitted=new CountDownLatch(1);
        PackedDanmakuReader concurrent=PackedDanmakuReader.copyOf(new int[]{1,0,7,0,1,0,0},new byte[0]);
        Thread worker=new Thread(()->{try {
            admitted.countDown();
            for(int i=0;i<10000;++i) {
                try { check(concurrent.weightOr(0,-1)==7); }
                catch(IllegalStateException expected) { break; }
            }
        } catch(Throwable problem) { failure.set(problem); }});
        worker.start(); admitted.await(); concurrent.close();worker.join();
        check(failure.get()==null && concurrent.isClosed()); ++cases;
        System.out.println("{\"suite\":\"packed-reader-java-contract\",\"cases\":"+cases+",\"status\":\"PASS\"}");
    }
}
