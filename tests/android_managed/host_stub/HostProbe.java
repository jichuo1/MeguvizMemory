import android.app.Instrumentation;
import io.github.jichuo1.meguviz.memory.android.ManagedInstrumentation;
public final class HostProbe {
    public static void main(String[] args) {
        new ManagedInstrumentation().onStart();
        if (Instrumentation.result == null) { System.out.println("{\"host_status\":\"NO_REPORT\"}"); System.exit(1); }
        String report = Instrumentation.result.getString("report_json");
        System.out.println(report == null ? "{\"host_status\":\"NO_REPORT\"}" : report);
        if (Instrumentation.code != 0) System.exit(1);
    }
}
