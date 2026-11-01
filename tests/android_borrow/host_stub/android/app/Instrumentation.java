package android.app;

import android.os.Bundle;

public class Instrumentation {
    public static int code=-999;
    public static Bundle result;
    public void onCreate(Bundle args) {}
    public void start() {}
    public void onStart() {}
    public void finish(int value,Bundle output) { code=value;result=output; }
}
