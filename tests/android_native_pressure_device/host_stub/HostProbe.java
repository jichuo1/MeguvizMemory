import android.app.Instrumentation;
import io.github.jichuo1.meguviz.memory.android.NativePressureCloseInstrumentation;

public final class HostProbe {
    public static void main(String[] args) {
        new NativePressureCloseInstrumentation().onStart();
        String report = Instrumentation.result == null ? null : Instrumentation.result.getString("report_json");
        System.out.println(report == null ? "{\"host_status\":\"NO_REPORT\"}" : report);
        if (!Instrumentation.finished || Instrumentation.statusSent || Instrumentation.code != 0 ||
            report == null || !report.contains("\"status\":\"PASS\"") ||
            !report.contains("\"checks\":53") || !report.contains("\"case_count\":4")) System.exit(1);
    }
}
