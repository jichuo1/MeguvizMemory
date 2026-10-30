package io.github.jichuo1.meguviz.memory.android;

import android.app.Application;
import android.app.Instrumentation;
import android.os.Bundle;
import android.os.Looper;
import org.json.JSONObject;
import android.os.ParcelFileDescriptor;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.concurrent.atomic.AtomicReference;

/** Isolated framework dispatch qualification, never a low-RAM stress test. */
public final class PressureInstrumentation extends Instrumentation {
    private int checks;
    private void check(boolean value,String reason) {++checks;if(!value)throw new AssertionError(reason);}
    @Override public void onCreate(Bundle arguments) {super.onCreate(arguments);start();}
    private static long[] available(NativePressureBackend backend) throws InterruptedException {
        long until=System.nanoTime()+3_000_000_000L;
        while(System.nanoTime()<until) {long[] value=backend.snapshot();if(value!=null)return value;Thread.sleep(5);}
        throw new AssertionError("native snapshot unavailable");
    }
    private String injectTrim(String level) throws Exception {
        String process=getTargetContext().getPackageName();
        if(!process.matches("[A-Za-z0-9_.]+"))throw new IllegalStateException("unexpected diagnostic package");
        ParcelFileDescriptor descriptor=getUiAutomation().executeShellCommand("am send-trim-memory "+process+" "+level);
        AtomicReference<String> result=new AtomicReference<>(),failure=new AtomicReference<>();
        Thread reader=new Thread(()->{
            try(InputStream input=new ParcelFileDescriptor.AutoCloseInputStream(descriptor);
                ByteArrayOutputStream bytes=new ByteArrayOutputStream()) {
                byte[] buffer=new byte[1024];int count;
                while((count=input.read(buffer))!=-1){if(bytes.size()+count>16384)throw new IllegalStateException("bounded command output exceeded");bytes.write(buffer,0,count);}
                result.set(bytes.toString("UTF-8"));
            }catch(Exception error){failure.set(error.getClass().getSimpleName());}
        },"memory-trim-command-reader");
        reader.start();reader.join(3000);
        if(reader.isAlive()){descriptor.close();reader.join(1000);throw new IllegalStateException("trim command deadline");}
        if(failure.get()!=null)throw new IllegalStateException("trim command read unavailable");
        String text=result.get()==null?"":result.get();
        if(text.contains("Error")||text.contains("Unknown")||text.contains("Exception"))throw new IllegalStateException("AMS trim rejected");
        return text;
    }
    private boolean qualifyAmsDispatch() {
        NativePressureBackend nativeBackend=null;MemoryPressureAdapter callback=null;
        try {
            nativeBackend=NativePressureBackend.createArena(4096,2048,0,256);
            callback=new MemoryPressureAdapter(getTargetContext(),nativeBackend);
            long before=callback.forwardedEvents();injectTrim("RUNNING_LOW");
            long deadline=System.nanoTime()+3_000_000_000L;
            while(callback.forwardedEvents()==before&&System.nanoTime()<deadline)Thread.sleep(5);
            if(callback.forwardedEvents()<=before||available(nativeBackend)[2]!=2048)return false;
            before=callback.forwardedEvents();injectTrim("RUNNING_CRITICAL");
            deadline=System.nanoTime()+3_000_000_000L;
            while(callback.forwardedEvents()==before&&System.nanoTime()<deadline)Thread.sleep(5);
            return callback.forwardedEvents()>before&&available(nativeBackend)[2]==0;
        }catch(Exception | AssertionError unavailable){return false;}
        finally {if(callback!=null){callback.close();callback.awaitClosed(5000);}else if(nativeBackend!=null){nativeBackend.close();nativeBackend.awaitClosed(5000);}}
    }
    @Override public void onStart() {
        Bundle results=new Bundle();NativePressureBackend backend=null;MemoryPressureAdapter adapter=null;
        NativePressureBackend.LeaseProbe probe=null;
        try {
            check(Looper.myLooper()!=Looper.getMainLooper(),"qualification must be a worker");
            backend=NativePressureBackend.createArena(4096,2048,0,256);
            adapter=new MemoryPressureAdapter(getTargetContext(),backend);
            Application application=(Application)getTargetContext().getApplicationContext();
            probe=backend.holdRetiringLeaseForDiagnostics(1024);
            long[] initial=available(backend);
            check(initial.length==16 && initial[3]==1024 && initial[6]==1024,"retiring lease charged once");
            application.onTrimMemory(10); // Real Application callback dispatch; synthetic event.
            check(adapter.forwardedEvents()==1,"registered framework callback delivered");
            check(available(backend)[2]==2048,"moderate budget applied");
            application.onTrimMemory(20);
            check(adapter.forwardedEvents()==1 && adapter.ignoredLifecycleEvents()==1,"UI hidden not pressure");
            application.onTrimMemory(15);
            check(adapter.forwardedEvents()==2,"critical callback delivered");
            long until=System.nanoTime()+3_000_000_000L;long[] pressured=available(backend);
            while(pressured[12]==0 && System.nanoTime()<until) {Thread.sleep(5);pressured=available(backend);}
            check(pressured[2]==0 && pressured[3]==1024 && pressured[6]==1024 && pressured[8]==1024,"soft budget preserves retiring storage");
            check(pressured[12]>0,"trim deferred while lease held");
            check(probe.readable(),"lease readable under pressure");
            check(adapter.restoreBudget()==0 && available(backend)[2]==4096,"restore explicit");
            ProcessMemoryObservation memory=ProcessMemoryObservation.capture();
            check(memory.pssKb>=0 || memory.androidMemoryUnavailableReason!=null,"PSS unavailable is explicit");
            check(memory.rssKb>=0 || memory.rssUnavailableReason!=null,"RSS unavailable is explicit");
            adapter.close();
            check(adapter.isClosed(),"adapter callback admission closed");
            check(probe.readable(),"lease survives adapter close");
            check(!adapter.awaitClosed(0),"poll does not force close live lease");
            check(!adapter.awaitClosed(15),"bounded wait times out with live lease");
            long accepted=adapter.forwardedEvents();application.onTrimMemory(10);
            check(adapter.forwardedEvents()==accepted,"closed callback never forwards");
            probe.close();probe=null;
            check(adapter.awaitClosed(5000),"lease completion drains owned domain");
            long[] end=backend.snapshot();
            check(end!=null && end[0]==2 && end[1]==2 && end[3]==0 && end[4]==0 && end[5]==0 && end[6]==0,"final core/controller zero");
            adapter.close();check(adapter.awaitClosed(0),"double close stable");
            JSONObject report=new JSONObject();report.put("suite","android-pressure-v1");report.put("status","PASS");report.put("checks",checks);
            boolean ams=qualifyAmsDispatch();
            report.put("framework_dispatch","APPLICATION_DISPATCH_SYNTHETIC_TRIM_EVENT");
            report.put("ams_injected_callback_status",ams?"PASS":"BLOCKED_UNSUPPORTED_OR_NO_DELIVERY");
            report.put("ams_injected_callback_scope","OWN_DIAGNOSTIC_PACKAGE_ONLY_NOT_REAL_DEVICE_LOW_RAM");
            report.put("actual_low_memory_event",false);report.put("logical_scope","Memory requested backing; not RSS/PSS");
            report.put("rss_kb",memory.rssKb>=0?memory.rssKb:JSONObject.NULL);report.put("rss_unavailable_reason",memory.rssUnavailableReason==null?JSONObject.NULL:memory.rssUnavailableReason);
            report.put("pss_kb",memory.pssKb>=0?memory.pssKb:JSONObject.NULL);report.put("private_dirty_kb",memory.privateDirtyKb>=0?memory.privateDirtyKb:JSONObject.NULL);
            report.put("android_memory_unavailable_reason",memory.androidMemoryUnavailableReason==null?JSONObject.NULL:memory.androidMemoryUnavailableReason);
            report.put("energy_joules",JSONObject.NULL);report.put("energy_unavailable_reason",memory.energyUnavailableReason);
            report.put("final_backing_bytes",end[3]);report.put("performance_qualified",false);
            results.putString("report_json",report.toString());finish(0,results);
        } catch(Throwable error) {
            results.putString("report_json","{\"suite\":\"android-pressure-v1\",\"status\":\"FAIL\",\"checks\":"+checks+",\"error_type\":\""+error.getClass().getSimpleName()+"\"}");
            results.putString("failure",error.toString());finish(1,results);
        } finally {
            if(probe!=null)probe.close();if(adapter!=null)adapter.close();else if(backend!=null)backend.close();
        }
    }
}
