package org.json;

import java.util.LinkedHashMap;
import java.util.Map;

/** Only the types used by this isolated diagnostic are supported. */
public class JSONObject {
    private final Map<String,Object> fields=new LinkedHashMap<>();
    public JSONObject put(String key,Object value) { fields.put(key,value);return this; }
    private static String quote(String value) {
        return "\""+value.replace("\\","\\\\").replace("\"","\\\"")
            .replace("\n","\\n").replace("\r","\\r").replace("\t","\\t")+"\"";
    }
    @Override public String toString() {
        StringBuilder result=new StringBuilder("{");
        for(Map.Entry<String,Object> entry:fields.entrySet()) {
            if(result.length()>1)result.append(',');
            result.append(quote(entry.getKey())).append(':');
            Object value=entry.getValue();
            if(value instanceof String)result.append(quote((String)value));
            else if(value instanceof Boolean || value instanceof Number)result.append(value);
            else throw new IllegalStateException("unsupported fixture value");
        }
        return result.append('}').toString();
    }
}
