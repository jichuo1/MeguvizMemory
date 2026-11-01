package android.app;
import android.content.Context;
import android.os.Bundle;
public class Instrumentation {
    public static int code = -999;
    public static Bundle result;
    public static boolean finished, statusSent;
    private final Context context = new Context();
    public void onCreate(Bundle args) {}
    public void start() {}
    public void onStart() {}
    public Context getTargetContext() { return context; }
    public void finish(int value, Bundle output) { finished = true; code = value; result = output; }
    public void sendStatus(int value, Bundle output) { statusSent = true; code = value; result = output; }
}
