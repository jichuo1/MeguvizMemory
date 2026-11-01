import android.app.Instrumentation;
import io.github.jichuo1.meguviz.memory.android.NativePressureCloseInstrumentation;

/** Fake JNI lacks the probe method, modeling an unconfirmed native outcome. */
public final class HostIncompleteProbe {
    public static void main(String[] args) {
        new NativePressureCloseInstrumentation().onStart();
        String report = Instrumentation.result == null ? null : Instrumentation.result.getString("report_json");
        System.out.println(report == null ? "{\"host_status\":\"NO_REPORT\"}" : report);
        if (Instrumentation.finished || !Instrumentation.statusSent || Instrumentation.code != 1 ||
            report == null || !report.contains("\"status\":\"INCOMPLETE\"") ||
            !report.contains("\"stage\":\"create\"")) System.exit(1);
    }
}
