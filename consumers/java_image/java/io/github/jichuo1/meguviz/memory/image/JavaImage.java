package io.github.jichuo1.meguviz.memory.image;

import io.github.jichuo1.meguviz.memory.android.NativeMemoryLease;
import java.io.BufferedInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.PushbackInputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.FutureTask;

/** Independent file consumer of an installed Memory runtime and JNI adapter. */
public final class JavaImage {
    static {
        System.loadLibrary("mgmem_pressure_jni");
        System.loadLibrary("mgmem_java_image");
    }
    private static final long MAX_PIXELS = 8L * 1024 * 1024;
    private static final long MAX_BUDGET = 64L * 1024 * 1024;
    // Divisible by three and less than the JNI staging limit.
    private static final int TRANSFER_BYTES = 24 * 1024;
    private static native long nativeOpen(long bytes, long budget, boolean system);
    private static native long nativeRegister(long session);
    private static native int nativeRetire(long session);
    private static native int nativeCancel(long registration);
    private static native long nativeFinish(long session);
    private static native int nativeActiveSessions();
    private static native String[] nativeModulePaths();

    private static IllegalStateException status(String action, long code) {
        return new IllegalStateException(action + " failed: " + code);
    }
    private static final class Session implements AutoCloseable {
        private long id, registration;
        private NativeMemoryLease lease;
        private long peak;
        static Session open(long bytes, long budget, boolean system) {
            Session result = new Session();
            result.id = nativeOpen(bytes, budget, system);
            if (result.id <= 0) throw status("open", -result.id);
            try {
                result.registration = nativeRegister(result.id);
                if (result.registration <= 0) {
                    long code = -result.registration;
                    result.registration = 0;
                    throw status("register", code);
                }
                result.lease = NativeMemoryLease.fromNativeRegistration(result.registration);
                result.registration = 0;
                int retired = nativeRetire(result.id);
                if (retired != 0) throw status("retire", retired);
                return result;
            } catch (RuntimeException | Error error) {
                try { result.close(); } catch (RuntimeException | Error cleanup) { error.addSuppressed(cleanup); }
                throw error;
            }
        }
        @Override public void close() {
            if (id == 0) return;
            Throwable problem = null;
            if (lease != null) {
                try { lease.close(); } catch (RuntimeException | Error error) { problem = error; }
                finally { lease = null; }
            }
            if (registration != 0) {
                int cancelled = nativeCancel(registration);
                registration = 0; // A failed cancellation may have consumed the registration.
                if (cancelled != 0) {
                    Throwable error = status("cancel", cancelled);
                    if (problem == null) problem = error; else problem.addSuppressed(error);
                }
            }
            long finished = nativeFinish(id);
            if (finished < 0) {
                Throwable error = status("finish", -finished);
                if (problem == null) problem = error; else problem.addSuppressed(error);
            } else {
                peak = finished;
                id = 0;
            }
            if (problem instanceof Error) throw (Error) problem;
            if (problem != null) throw (RuntimeException) problem;
        }
    }
    private static boolean whitespace(int c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }
    private static String token(PushbackInputStream input) throws IOException {
        StringBuilder result = new StringBuilder();
        int c;
        while ((c = input.read()) >= 0) {
            if (c == '#') { while ((c = input.read()) >= 0 && c != '\n') {} continue; }
            if (!whitespace(c)) { result.append((char)c); break; }
        }
        while ((c = input.read()) >= 0) {
            if (whitespace(c)) {
                if (c == '\r') { int next = input.read(); if (next >= 0 && next != '\n') input.unread(next); }
                break;
            }
            if (result.length() >= 32) throw new IOException("oversized PPM token");
            result.append((char)c);
        }
        if (result.length() == 0) throw new IOException("truncated PPM header");
        return result.toString();
    }
    private static long number(String value) throws IOException {
        long n = 0;
        for (int i=0;i<value.length();++i) {
            char c=value.charAt(i);
            if (c<'0' || c>'9' || n>(Long.MAX_VALUE-9)/10) throw new IOException("invalid PPM number");
            n=n*10+(c-'0');
        }
        return n;
    }
    private static int readSome(InputStream input, byte[] buffer, int length) throws IOException {
        int count = input.read(buffer, 0, length);
        if (count == 0) { int b=input.read(); if (b<0) return -1; buffer[0]=(byte)b; return 1; }
        return count;
    }
    private static void loadInput(InputStream input, Session session, long bytes, byte[] transfer) throws IOException {
        long offset = 0;
        while (offset < bytes) {
            int count = readSome(input, transfer, (int)Math.min(transfer.length, bytes-offset));
            if (count < 0) throw new IOException("truncated RGB input");
            session.lease.write(offset, transfer, 0, count);
            offset += count;
        }
        if (input.read() != -1) throw new IOException("trailing RGB input");
    }
    private static final class Result {
        final byte[] pixels;
        final long checksum;
        Result(byte[] pixels,long checksum) {this.pixels=pixels;this.checksum=checksum;}
    }
    private static Result calculate(NativeMemoryLease lease, int width, int height, byte[] transfer) throws InterruptedException {
        final int pixels=width*height;
        byte[] luma=new byte[pixels], output=new byte[pixels];
        int position=0;
        while (position < pixels) {
            if (Thread.currentThread().isInterrupted()) throw new InterruptedException("image worker interrupted");
            int batch=Math.min(transfer.length/3,pixels-position), bytes=batch*3;
            lease.read((long)position*3,transfer,0,bytes);
            for (int i=0;i<batch;++i) {
                int j=3*i;
                luma[position+i]=(byte)((77*(transfer[j]&255)+150*(transfer[j+1]&255)+29*(transfer[j+2]&255))>>8);
            }
            position+=batch;
        }
        long checksum=0;
        for (int y=0;y<height;++y) {
            if (Thread.currentThread().isInterrupted()) throw new InterruptedException("image worker interrupted");
            for (int x=0;x<width;++x) {
                int sum=0;
                for (int dy=-1;dy<=1;++dy) for (int dx=-1;dx<=1;++dx) {
                    int yy=dy<0?(y>0?y-1:0):(dy>0&&y+1<height?y+1:y);
                    int xx=dx<0?(x>0?x-1:0):(dx>0&&x+1<width?x+1:x);
                    sum+=luma[yy*width+xx]&255;
                }
                int value=sum/9;
                output[y*width+x]=(byte)value;
                checksum+=value;
            }
        }
        return new Result(output,checksum);
    }
    private static Result onWorker(NativeMemoryLease lease,int width,int height,byte[] transfer) throws Exception {
        // The native lease is already held before the task is constructed or started.
        FutureTask<Result> task=new FutureTask<>(()->calculate(lease,width,height,transfer));
        Thread worker=new Thread(task,"mgmem-java-image");
        worker.start();
        boolean interrupted=false;
        while (worker.isAlive()) {
            try {worker.join();}
            catch (InterruptedException error) {interrupted=true;worker.interrupt();}
        }
        if (interrupted) {Thread.currentThread().interrupt();throw new InterruptedException("image consumer interrupted");}
        try {return task.get();}
        catch (ExecutionException error) {
            Throwable cause=error.getCause();
            if (cause instanceof Exception) throw (Exception)cause;
            if (cause instanceof Error) throw (Error)cause;
            throw new IllegalStateException("worker failed",cause);
        }
    }
    private static String hash(InputStream input) throws Exception {
        MessageDigest digest=MessageDigest.getInstance("SHA-256");
        byte[] bytes=new byte[8192];int count;
        while ((count=input.read(bytes))!=-1) {if (count>0) digest.update(bytes,0,count);}
        char[] digits="0123456789abcdef".toCharArray(),output=new char[64];
        byte[] value=digest.digest();
        for(int i=0;i<value.length;++i) {output[2*i]=digits[(value[i]&255)>>>4];output[2*i+1]=digits[value[i]&15];}
        return new String(output);
    }
    private static String hashFile(Path path) throws Exception {
        try (InputStream stream=Files.newInputStream(path)) {return hash(stream);}
    }
    private static final class Identity {
        final String core,jni,jar,javaClass;
        Identity(String core,String jni,String jar,String javaClass) {this.core=core;this.jni=jni;this.jar=jar;this.javaClass=javaClass;}
    }
    private static Identity attest(Path core,Path jni,Path jar) throws Exception {
        String[] loaded=nativeModulePaths();
        if(loaded==null || loaded.length!=2 || loaded[0]==null || loaded[1]==null ||
                !Files.isSameFile(Paths.get(loaded[0]),core) || !Files.isSameFile(Paths.get(loaded[1]),jni))
            throw new IllegalStateException("loaded native modules do not match the expected installed files");
        if(NativeMemoryLease.class.getProtectionDomain()==null ||
                NativeMemoryLease.class.getProtectionDomain().getCodeSource()==null)
            throw new IllegalStateException("Java adapter code source unavailable");
        Path loadedJar=Paths.get(NativeMemoryLease.class.getProtectionDomain().getCodeSource().getLocation().toURI());
        if(!Files.isSameFile(loadedJar,jar)) throw new IllegalStateException("loaded Java adapter is not from the expected JAR");
        String resource="io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.class";
        String jarClass,loadedClass;
        try(JarFile file=new JarFile(jar.toFile())) {
            JarEntry entry=file.getJarEntry(resource);
            if(entry==null) throw new IllegalStateException("expected JAR does not contain the adapter class");
            try(InputStream input=file.getInputStream(entry)) {jarClass=hash(input);}
        }
        try(InputStream input=NativeMemoryLease.class.getResourceAsStream("NativeMemoryLease.class")) {
            if(input==null) throw new IllegalStateException("loaded adapter class resource unavailable");
            loadedClass=hash(input);
        }
        if(!jarClass.equals(loadedClass)) throw new IllegalStateException("adapter class bytes do not match the expected JAR");
        return new Identity(hashFile(core),hashFile(jni),hashFile(jar),loadedClass);
    }
    private static void execute(String[] args) throws Exception {
        if (args.length<2) throw new IllegalArgumentException("usage: JavaImage input.ppm output.pgm [--system] [--budget bytes]");
        Path inputPath=Paths.get(args[0]),outputPath=Paths.get(args[1]);
        boolean system=false;long budget=MAX_BUDGET;
        Path expectedCore=null,expectedJni=null,expectedJar=null;
        for (int i=2;i<args.length;++i) {
            String option=args[i];
            if ("--system".equals(option)) {system=true;continue;}
            if (i+1>=args.length) throw new IllegalArgumentException("unknown or incomplete option");
            if ("--budget".equals(option)) budget=number(args[++i]);
            else if ("--attest-core".equals(option)) expectedCore=Paths.get(args[++i]);
            else if ("--attest-jni".equals(option)) expectedJni=Paths.get(args[++i]);
            else if ("--attest-jar".equals(option)) expectedJar=Paths.get(args[++i]);
            else throw new IllegalArgumentException("unknown or incomplete option");
        }
        if (budget<=0 || budget>MAX_BUDGET) throw new IllegalArgumentException("budget outside supported range");
        int expectedCount=(expectedCore!=null?1:0)+(expectedJni!=null?1:0)+(expectedJar!=null?1:0);
        if(expectedCount!=0 && expectedCount!=3) throw new IllegalArgumentException("all three identity inputs are required");
        Identity identity=expectedCount==3?attest(expectedCore,expectedJni,expectedJar):null;
        int width,height;long rgbBytes;Result result;Session session;
        byte[] transfer=new byte[TRANSFER_BYTES];
        try (PushbackInputStream input=new PushbackInputStream(new BufferedInputStream(Files.newInputStream(inputPath)),1)) {
            if (!"P6".equals(token(input))) throw new IOException("expected a P6 PPM input");
            long w=number(token(input)),h=number(token(input)),max=number(token(input));
            if (max!=255 || w<=0 || h<=0 || w>MAX_PIXELS || h>MAX_PIXELS || w>MAX_PIXELS/h)
                throw new IOException("unsupported dimensions or pixel format");
            long pixels=w*h;rgbBytes=pixels*3;
            width=(int)w;height=(int)h;
            session=Session.open(rgbBytes,budget,system);
            try (Session ignored=session) {
                loadInput(input,session,rgbBytes,transfer);
                result=onWorker(session.lease,width,height,transfer);
            }
        }
        if(nativeActiveSessions()!=0) throw new IllegalStateException("native image session cleanup not confirmed");
        // The Java result survives the native lease; do not overwrite an existing file.
        try (OutputStream output=Files.newOutputStream(outputPath,StandardOpenOption.CREATE_NEW,StandardOpenOption.WRITE)) {
            output.write(("P5\n"+width+" "+height+"\n255\n").getBytes(StandardCharsets.US_ASCII));
            output.write(result.pixels);
        }
        System.out.println("{\"consumer\":\"java_ppm_luma_box_blur\",\"success\":true,\"mode\":\""+
            (system?"system":"arena")+"\",\"pixels\":"+((long)width*height)+",\"checksum\":"+result.checksum+
            ",\"native_peak_backing_bytes\":"+session.peak+",\"native_final_backing_bytes\":0"+
            ",\"native_initialized_bytes\":"+rgbBytes+",\"source_to_java_bytes\":"+rgbBytes+
            ",\"java_to_native_staging_bytes\":"+rgbBytes+",\"staging_to_native_backing_bytes\":"+rgbBytes+
            ",\"native_to_java_bytes\":"+rgbBytes+",\"java_luma_and_output_bytes\":"+(2L*width*height)+
            ",\"transfer_buffer_bytes\":"+TRANSFER_BYTES+",\"lease_before_enqueue\":true,\"native_sessions_remaining\":0"+
            (identity==null?",\"identity_scope\":\"NOT_CHECKED\"":",\"identity_scope\":\"OS_MODULE_PATH_AND_CURRENT_FILE_HASH\",\"core_sha256\":\""+
                identity.core+"\",\"jni_sha256\":\""+identity.jni+"\",\"java_jar_sha256\":\""+identity.jar+"\",\"java_class_sha256\":\""+identity.javaClass+"\"")+
            ",\"scope\":\"HOST_FUNCTIONAL_NOT_PERFORMANCE\"}");
    }
    public static void main(String[] args) {
        try {execute(args);}
        catch (Throwable error) {
            System.err.println("Java image consumer: "+error.getClass().getSimpleName()+": "+error.getMessage());
            for (Throwable suppressed:error.getSuppressed()) System.err.println("cleanup: "+suppressed.getClass().getSimpleName()+": "+suppressed.getMessage());
            try {System.err.println("native_sessions_remaining="+nativeActiveSessions());}
            catch (Throwable unavailable) {System.err.println("native_sessions_remaining=UNKNOWN");}
            System.exit(1);
        }
    }
}
