package android.os;

public class Bundle {
    private String report;
    public void putString(String key,String value) { if ("report_json".equals(key)) report=value; }
    public String getString(String key) { return "report_json".equals(key) ? report : null; }
}
