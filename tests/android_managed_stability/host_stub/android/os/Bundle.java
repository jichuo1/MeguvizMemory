package android.os;
import java.util.HashMap;
public class Bundle {
    private final HashMap<String,String> values = new HashMap<>();
    public void putString(String key, String value) { values.put(key, value); }
    public String getString(String key) { return values.get(key); }
}
