package android.content;

import android.content.res.Configuration;

public interface ComponentCallbacks2 {
    void onTrimMemory(int level);
    void onLowMemory();
    void onConfigurationChanged(Configuration configuration);
}
