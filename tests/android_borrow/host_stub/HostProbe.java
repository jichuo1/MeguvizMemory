import android.app.Instrumentation;
import io.github.jichuo1.meguviz.memory.android.NativeBorrowInstrumentation;

/** Runs the real instrumentation methods with host-only Android stubs. */
public class HostProbe {
    public static void main(String[] args) {
        new NativeBorrowInstrumentation().onStart();
        if(Instrumentation.result==null)throw new AssertionError("missing report");
        String report=Instrumentation.result.getString("report_json");
        if(report==null)throw new AssertionError("missing report JSON");
        System.out.println("INSTRUMENTATION_RESULT: report_json="+report);
        System.out.println("INSTRUMENTATION_CODE: "+Instrumentation.code);
    }
}
