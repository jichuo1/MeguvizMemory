package io.github.jichuo1.meguviz.memory.android;
public final class ManagedMemoryDomainPlainJvmTest {
    public static void main(String[] args) throws Exception {
        for (int i = 0; i < 2; ++i) {
            ManagedMemoryDomain d = i == 0 ? ManagedMemoryDomain.system(8192) : ManagedMemoryDomain.arena(8192, 4096);
            ManagedMemoryDomain.Allocation owner = d.allocate(64, 8);
            ManagedMemoryDomain.Lease lease = owner.borrowWrite(0, 64);
            byte[] data = new byte[64]; lease.read(0, data, 0, data.length);
            for (byte value : data) if (value != 0) throw new AssertionError("zero initialization");
            lease.write(0, new byte[]{9}, 0, 1); owner.close(); d.beginClose();
            lease.read(0, data, 0, 1); if (data[0] != 9) throw new AssertionError("lease lifetime");
            lease.close(); long until = System.nanoTime() + 8_000_000_000L;
            while (d.state() != ManagedMemoryDomain.CLOSED && System.nanoTime() - until < 0) Thread.sleep(2);
            if (d.state() != ManagedMemoryDomain.CLOSED) throw new AssertionError("close");
            d.forgetClosed();
        }
    }
}
