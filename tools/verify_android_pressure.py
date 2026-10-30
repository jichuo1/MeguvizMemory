#!/usr/bin/env python3
"""Compile Android Java sources and run framework-stub lifecycle regressions.

No ADB, native loading or actual Android pressure measurement occurs here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
JAVA=ROOT/'adapters/android/java'
STUBS={
'android/content/ComponentCallbacks2.java':'''package android.content; import android.content.res.Configuration; public interface ComponentCallbacks2 {void onTrimMemory(int level);void onLowMemory();void onConfigurationChanged(Configuration c);}''',
'android/content/res/Configuration.java':'package android.content.res; public final class Configuration {}',
'android/content/Context.java':'''package android.content; public abstract class Context {public Context getApplicationContext(){return this;} public abstract void registerComponentCallbacks(ComponentCallbacks2 c);public abstract void unregisterComponentCallbacks(ComponentCallbacks2 c);}''',
'android/os/Looper.java':'''package android.os; public final class Looper {private static final Thread MAIN_THREAD=Thread.currentThread();private static final Looper MAIN=new Looper();public static Looper getMainLooper(){return MAIN;}public static Looper myLooper(){return Thread.currentThread()==MAIN_THREAD?MAIN:null;}}''',
'android/os/SystemClock.java':'package android.os; public final class SystemClock {public static long elapsedRealtimeNanos(){return System.nanoTime();}}',
'android/os/Debug.java':'''package android.os; public final class Debug {public static final class MemoryInfo {public int getTotalPss(){return 123;}public int getTotalPrivateDirty(){return 17;}}public static void getMemoryInfo(MemoryInfo info){}}''',
}
TEST='''
import io.github.jichuo1.meguviz.memory.android.*;
import android.content.*;
import android.os.Looper;
import java.util.ArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
public final class PressureJavaChecks {
 static int checks; static void ok(boolean value,String reason){++checks;if(!value)throw new AssertionError(reason);}
 static final class ContextSpy extends Context {
  int registrations,unregistrations;boolean failRegister,failUnregister;
  final ArrayList<ComponentCallbacks2> callbacks=new ArrayList<>();
  public void registerComponentCallbacks(ComponentCallbacks2 c){registrations++;if(failRegister)throw new IllegalStateException("register");callbacks.add(c);}
  public void unregisterComponentCallbacks(ComponentCallbacks2 c){unregistrations++;if(failUnregister){failUnregister=false;throw new IllegalStateException("unregister");}callbacks.remove(c);}
  void event(int n){for(ComponentCallbacks2 c:new ArrayList<>(callbacks))c.onTrimMemory(n);}
 }
 static class Fake implements MemoryPressureAdapter.Backend {
  int events,last=-1,closes;boolean closed,failClose;Runnable onNotify;
  public int notifyPressure(int n){if(closed)return 7;events++;last=n;if(onNotify!=null)onNotify.run();return 0;}
  public void beginClose(){closes++;if(failClose){failClose=false;throw new IllegalStateException("cleanup admission");}closed=true;}
  public boolean awaitClosed(long n){return closed;}
  public long[] snapshot(){return new long[]{123};}
 }
 public static void main(String[] args)throws Exception {
  Looper.getMainLooper();
  int[] levels={0,4,5,10,11,14,15,19,20,39,40,79,80,90};
  int[] expected={-1,-1,1,1,1,1,2,2,-1,-1,1,1,2,2};
  for(int i=0;i<levels.length;i++)ok(MemoryPressureAdapter.classify(levels[i])==expected[i],"range mapping");
  ContextSpy context=new ContextSpy();Fake backend=new Fake();MemoryPressureAdapter a=new MemoryPressureAdapter(context,backend);
  ok(context.registrations==1,"registered once");context.event(10);ok(backend.events==1&&backend.last==1,"moderate callback");
  context.event(20);ok(backend.events==1&&a.ignoredLifecycleEvents()==1,"UI hidden ignored");context.event(15);ok(backend.last==2,"critical callback");
  a.onLowMemory();ok(backend.last==2,"compat low memory");ok(a.restoreBudget()==0&&backend.last==0,"explicit restore");
  a.close();a.close();ok(context.unregistrations==1&&backend.closes==1,"close idempotent");context.event(10);a.onTrimMemory(10);ok(backend.events==4,"closed callbacks rejected");
  ok(a.awaitClosed(0),"main zero poll");boolean denied=false;try{a.awaitClosed(1);}catch(IllegalStateException e){denied=true;}ok(denied,"main positive timeout rejected");
  Thread waiter=new Thread(()->ok(a.awaitClosed(5),"worker bounded wait"));waiter.start();waiter.join(1000);ok(!waiter.isAlive(),"worker completed");
  ContextSpy failContext=new ContextSpy();Fake failBackend=new Fake();failBackend.failClose=true;MemoryPressureAdapter retry=new MemoryPressureAdapter(failContext,failBackend);
  denied=false;try{retry.close();}catch(IllegalStateException e){denied=true;}ok(denied&&retry.isClosed(),"cleanup failure visible and callback admission closed");
  ok(retry.requestPressure(2)==7,"no events after failed cleanup admission");retry.close();ok(failContext.unregistrations==1&&failBackend.closes==2&&failBackend.closed,"cleanup admission retry without duplicate unregister");
  ContextSpy u=new ContextSpy();u.failUnregister=true;Fake ub=new Fake();MemoryPressureAdapter ua=new MemoryPressureAdapter(u,ub);
  try{ua.close();}catch(IllegalStateException expectedFailure){}ua.close();ok(u.unregistrations==2&&ub.closes==1,"unregister retry does not duplicate successful cleanup");
  ContextSpy bad=new ContextSpy();bad.failRegister=true;Fake bb=new Fake();denied=false;try{new MemoryPressureAdapter(bad,bb);}catch(IllegalStateException e){denied=true;}ok(denied&&bb.closed,"registration failure closes backend");
  ContextSpy reentrantContext=new ContextSpy();Fake rb=new Fake();MemoryPressureAdapter reentrant=new MemoryPressureAdapter(reentrantContext,rb);rb.onNotify=reentrant::close;
  reentrant.onTrimMemory(10);ok(reentrant.isClosed()&&rb.closes==1,"close from callback does not deadlock");
  CountDownLatch accepted=new CountDownLatch(1),release=new CountDownLatch(1);ContextSpy racingContext=new ContextSpy();Fake racing=new Fake();
  racing.onNotify=()->{accepted.countDown();try{release.await(3,TimeUnit.SECONDS);}catch(InterruptedException e){throw new AssertionError(e);}};
  MemoryPressureAdapter ra=new MemoryPressureAdapter(racingContext,racing);Thread event=new Thread(()->ra.onTrimMemory(10));event.start();ok(accepted.await(1,TimeUnit.SECONDS),"event admitted");
  Thread closer=new Thread(ra::close);closer.start();closer.join(500);ok(!closer.isAlive(),"close does not wait on in-flight callback");release.countDown();event.join(1000);ok(!event.isAlive(),"in-flight callback can complete");
  ProcessMemoryObservation observation=ProcessMemoryObservation.capture();ok(observation.pssKb==123&&observation.privateDirtyKb==17,"stub readings separate from logical snapshot");
  ok(observation.rssKb>=0||observation.rssUnavailableReason!=null,"RSS unavailable has reason");ok(observation.energyJoules==null&&observation.energyUnavailableReason!=null,"energy unavailable not zero");
  System.out.println("{\\"suite\\":\\"pressure-java-stubs\\",\\"status\\":\\"PASS\\",\\"checks\\":"+checks+",\\"actual_device_operations\\":0}");
 }
}
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java-home',type=Path)
    parser.add_argument('--android-jar',type=Path)
    parser.add_argument('--out-dir',type=Path,required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args()
    suffix='.exe' if __import__('os').name=='nt' else ''
    javac=str(args.java_home/'bin'/('javac'+suffix)) if args.java_home else shutil.which('javac')
    java=str(args.java_home/'bin'/('java'+suffix)) if args.java_home else shutil.which('java')
    if not javac or not java:parser.error('Existing JDK is required')
    out=args.out_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    sources=[]
    for name,body in STUBS.items():
        path=out/'stubs'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body,encoding='utf-8');sources.append(path)
    test=out/'PressureJavaChecks.java';test.write_text(TEST,encoding='utf-8')
    classes=out/'classes';classes.mkdir()
    library=sorted(JAVA.rglob('*.java'))
    result=subprocess.run([javac,'--release','8','-d',str(classes)]+list(map(str,sources+library+[test])),capture_output=True,text=True,check=False)
    (out/'javac.stdout').write_text(result.stdout,encoding='utf-8');(out/'javac.stderr').write_text(result.stderr,encoding='utf-8')
    if result.returncode:raise RuntimeError('Java stub compilation failed; see local javac.stderr')
    run=subprocess.run([java,'-cp',str(classes),'PressureJavaChecks'],capture_output=True,text=True,timeout=20,check=False)
    (out/'tests.stdout').write_text(run.stdout,encoding='utf-8');(out/'tests.stderr').write_text(run.stderr,encoding='utf-8')
    if run.returncode:raise RuntimeError('Java lifecycle checks failed; see local tests.stderr')
    native=json.loads(run.stdout.strip())
    real_compile=False
    if args.android_jar:
        target=out/'android-api-classes';target.mkdir()
        command=[javac,'--release','8','-cp',str(args.android_jar),'-d',str(target)]+list(map(str,library))
        actual=subprocess.run(command,capture_output=True,text=True,check=False)
        (out/'android-javac.stderr').write_text(actual.stderr,encoding='utf-8')
        if actual.returncode:raise RuntimeError('Actual Android API compilation failed')
        real_compile=True
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    receipt={'schema_version':1,'scope':'FRAMEWORK_STUB_LIFECYCLE_TESTS_NOT_ANDROID_RUNTIME','status':'PASS',
             'checks':native['checks'],'actual_device_operations':0,'real_android_api_compile':real_compile,
             'sources':[{'path':p.relative_to(ROOT).as_posix(),'sha256':sha(p)} for p in library+[Path(__file__)]],
             'limitations':['Stub Debug.MemoryInfo values are synthetic, never residency measurements.',
                            'Actual callback delivery and JNI/domain close require Android device qualification.']}
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    args.receipt.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'status':'PASS','java_checks':native['checks'],'actual_android_api_compile':real_compile,'device_operations':0}))
if __name__=='__main__':main()
