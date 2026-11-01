package android.content;
import java.io.File;
public class Context {
    public File getCacheDir() {
        try { Thread.sleep(Long.getLong("test.stability.preparation.delay", 0)); }
        catch (InterruptedException error) { Thread.currentThread().interrupt(); }
        return new File(System.getProperty("test.stability.cache", "."));
    }
}
