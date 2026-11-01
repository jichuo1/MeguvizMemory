package android.util;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
public class AtomicFile {
    private final File base;
    public AtomicFile(File base) { this.base = base; }
    public File getBaseFile() { return base; }
    public FileOutputStream startWrite() throws IOException { return new FileOutputStream(base); }
    public void finishWrite(FileOutputStream stream) throws IOException { stream.close(); }
    public void failWrite(FileOutputStream stream) throws IOException { stream.close(); }
}
