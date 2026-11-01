package io.github.jichuo1.meguviz.memory.android;
import java.io.File;
import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

/** Host-only source-manifest contract, not proof of a compiled APK. */
public final class ManifestHostTest {
    public static void main(String[] args) throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance(); factory.setNamespaceAware(true);
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "");
        factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_SCHEMA, "");
        Element root = factory.newDocumentBuilder().parse(new File(System.getProperty("test.stability.manifest"))).getDocumentElement();
        if (!root.getAttribute("package").equals("io.github.jichuo1.meguviz.memory.stabilityprobe")) throw new AssertionError();
        NodeList list = root.getElementsByTagName("activity");
        if (list.getLength() != 1) throw new AssertionError();
        Element activity = (Element)list.item(0); String ns = "http://schemas.android.com/apk/res/android";
        if (!activity.getAttributeNS(ns, "name").equals("io.github.jichuo1.meguviz.memory.android.PressureR8ForegroundActivity") ||
            !activity.getAttributeNS(ns, "exported").equals("true") ||
            !activity.getAttributeNS(ns, "permission").equals("android.permission.DUMP")) throw new AssertionError();
        System.out.println("{\"suite\":\"managed-stability-host-manifest\",\"status\":\"PASS\"}");
    }
}
