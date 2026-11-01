package android.app;
import android.content.Context;
import android.os.Bundle;
public class Instrumentation {
    public static int code = -1;
    public static Bundle result;
    public void onCreate(Bundle args) {}
    public void start() {}
    public void onStart() {}
    public Context getTargetContext() { return new Context(); }
    public void finish(int c, Bundle r) { code = c; result = r; }
}
