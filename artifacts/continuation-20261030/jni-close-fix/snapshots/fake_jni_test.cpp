#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>
#if defined(JNI_REVIEW_SOURCE)
#include JNI_REVIEW_SOURCE
#else
#include "../adapters/android/native/pressure_jni.cpp"
#endif

namespace test {
struct Fixture {
    std::shared_ptr<Bridge> state;
    u64 id=100;
    std::atomic<unsigned> calls{0},global_deletes{0},attach_calls{0},detach_calls{0};
    std::atomic<bool> jni_unlocked{true},snapshot_reentered{true},foreign_unlocked{false};
    std::atomic<bool> detached{false},attach_fails{false},throw_once{false};
    std::atomic<bool> block_callback{false},entered{false},resume{false};
    std::mutex gate;std::condition_variable changed;
    std::atomic<unsigned> lifetime_retains{0},lifetime_releases{0};
    bool foreign_bind=false,foreign_snapshot=false;
    std::atomic<bool> foreign_actual_reentry{false};
};
Fixture *active=nullptr;
thread_local bool pending_exception=false;
JNINativeInterface_ env_functions{};
JNIInvokeInterface_ vm_functions{};
JNIEnv env{&env_functions};JavaVM vm{&vm_functions};
void operation() {
    if(!active || !active->state)return;
    if(active->state->mutex.try_lock())active->state->mutex.unlock();
    else active->jni_unlocked=false;
}
jint JNICALL get_env(JavaVM *,void **out,jint) {
    operation();*out=nullptr;
    if(active->detached)return JNI_EDETACHED;
    *out=&env;return JNI_OK;
}
jint JNICALL attach(JavaVM *,void **out,void *) {
    operation();++active->attach_calls;*out=nullptr;
    if(active->attach_fails)return JNI_ERR;
    *out=&env;return JNI_OK;
}
jint JNICALL detach(JavaVM *) {operation();++active->detach_calls;return JNI_OK;}
jsize JNICALL length(JNIEnv *,jarray) {operation();return 16;}
jobject JNICALL new_global(JNIEnv *,jobject obj) {operation();return obj;}
void JNICALL delete_global(JNIEnv *,jobject) {operation();++active->global_deletes;}
jclass JNICALL object_class(JNIEnv *,jobject) {operation();return reinterpret_cast<jclass>(4);}
jmethodID JNICALL method(JNIEnv *,jclass,const char *,const char *) {operation();return reinterpret_cast<jmethodID>(3);}
jint JNICALL java_vm(JNIEnv *,JavaVM **out) {operation();*out=&vm;return JNI_OK;}
jlongArray JNICALL new_array(JNIEnv *,jsize) {operation();return reinterpret_cast<jlongArray>(5);}
void JNICALL set_array(JNIEnv *,jlongArray,jsize,jsize,const jlong *) {operation();}
jboolean JNICALL exception_check(JNIEnv *) {operation();return pending_exception?JNI_TRUE:JNI_FALSE;}
void JNICALL exception_clear(JNIEnv *) {operation();pending_exception=false;}
void JNICALL call(JNIEnv *,jobject,jmethodID,va_list) {
    operation();++active->calls;
    const auto result=JNI_NAME(nativeSnapshot)(&env,nullptr,static_cast<jlong>(active->id));
    if(!result)active->snapshot_reentered=false;
    // Exercise a nested completion poll while this publisher still owns pins.
    JNI_NAME(nativePollCompletion)(&env,nullptr,static_cast<jlong>(active->id));
    if(active->throw_once.exchange(false))pending_exception=true;
    if(active->block_callback) {
        std::unique_lock<std::mutex> lock(active->gate);
        active->entered=true;active->changed.notify_all();
        active->changed.wait_for(lock,std::chrono::seconds(2),[]{return active->resume.load();});
    }
}
void initialize() {
    vm_functions.GetEnv=&get_env;vm_functions.AttachCurrentThread=&attach;vm_functions.DetachCurrentThread=&detach;
    env_functions.GetArrayLength=&length;env_functions.NewGlobalRef=&new_global;env_functions.DeleteGlobalRef=&delete_global;
    env_functions.GetObjectClass=&object_class;env_functions.GetMethodID=&method;env_functions.GetJavaVM=&java_vm;
    env_functions.NewLongArray=&new_array;env_functions.SetLongArrayRegion=&set_array;
    env_functions.ExceptionCheck=&exception_check;env_functions.ExceptionClear=&exception_clear;
    env_functions.CallVoidMethodV=&call;
}
void make_done(Fixture &f,mgmem_status status=MGMEM_OK) {
    f.state=std::make_shared<Bridge>();f.state->ready=true;f.state->closing=true;f.state->done=true;
    f.state->close_status=status;f.state->final_snapshot.state=MGMEM_CLOSED;f.state->final_snapshot.domain.state=MGMEM_CLOSED;
    f.state->vm=&vm;f.state->listener=reinterpret_cast<jobject>(1);
    f.state->final_buffer=reinterpret_cast<jlongArray>(2);f.state->completion_method=reinterpret_cast<jmethodID>(3);
    std::lock_guard<std::mutex> lock(registry_mutex);bridges.emplace(f.id,f.state);
}
bool exists(u64 id) {std::lock_guard<std::mutex> lock(registry_mutex);return bridges.count(id)!=0;}
void lifetime_retain(void *p) {++static_cast<Fixture *>(p)->lifetime_retains;}
void lifetime_release(void *p) {
    auto &f=*static_cast<Fixture *>(p);
    if(f.foreign_bind || f.foreign_snapshot) {
        // This branch actually enters the public JNI function unconditionally.
        // Old nativeBind deadlocks on the mutex already held by the reaper;
        // the runner owns a separate process and captures a bounded timeout.
        const auto phase_path=std::getenv("JNI_REVIEW_PHASE_FILE");
        if(phase_path) {std::ofstream phase(phase_path);phase<<"FOREIGN_RELEASE_ENTER_"<<(f.foreign_bind?"nativeBind":"nativeSnapshot")<<'\n';}
        if(f.foreign_bind)f.foreign_actual_reentry=JNI_NAME(nativeBind)(&env,nullptr,static_cast<jlong>(f.id),
            reinterpret_cast<jobject>(1),reinterpret_cast<jlongArray>(2))==MGMEM_BUSY;
        else f.foreign_actual_reentry=JNI_NAME(nativeSnapshot)(&env,nullptr,static_cast<jlong>(f.id))!=nullptr;
        if(phase_path) {std::ofstream phase(phase_path,std::ios::app);phase<<"FOREIGN_RELEASE_RETURNED\n";}
        ++f.lifetime_releases;return;
    }
    if(f.state->mutex.try_lock()) {
        f.state->mutex.unlock();f.foreign_unlocked=true;
        // A real JNI entry can reject a closing bind without deadlocking.
        // The old implementation fails the try-lock and never enters this
        // blocking entry, keeping its counterexample bounded.
        const auto result=JNI_NAME(nativeBind)(&env,nullptr,static_cast<jlong>(f.id),
            reinterpret_cast<jobject>(1),reinterpret_cast<jlongArray>(2));
        if(result!=MGMEM_BUSY)f.foreign_unlocked=false;
    }
    ++f.lifetime_releases;
}
}
int main(int argc,char **argv) {
    using namespace test;
    if(argc!=2)return 2;
    initialize();Fixture f;active=&f;const std::string test_case=argv[1];
    bool pass=false;
    if(test_case=="foreign-release" || test_case=="foreign-release-bind" || test_case=="foreign-release-snapshot") {
        f.foreign_bind=test_case=="foreign-release-bind";f.foreign_snapshot=test_case=="foreign-release-snapshot";
        mgmem_domain *domain=nullptr;
        mgmem_domain_options_v1 options{sizeof(options),1,1,0,MGMEM_SYSTEM,0,4096,0,nullptr};
        if(mgmem_domain_create_v1(&options,&domain)!=MGMEM_OK)return 3;
        mgmem_domain_lifetime_v1 life{sizeof(life),1,&f,&lifetime_retain,&lifetime_release};
        mgmem_pressure_options_v1 policy{sizeof(policy),1,4096,2048,0,1,0};
        if(mgmem_android_pressure_register_v1(domain,&life,&policy,&f.id)!=MGMEM_OK)return 3;
        f.state=bridge(f.id);
        if(mgmem_android_pressure_unregister_v1(f.id)!=MGMEM_OK)return 3;
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(f.foreign_bind?5:3);
        while(exists(f.id) && std::chrono::steady_clock::now()<deadline)std::this_thread::sleep_for(std::chrono::milliseconds(1));
        if(exists(f.id)) {
            // A callback is still executing. Never destroy its foreign domain or
            // stack context; fail-stop this isolated diagnostic process.
            std::cout<<"{\"case\":\""<<test_case<<"\",\"pass\":false,\"state\":\"INCOMPLETE_CALLBACK_STILL_ACTIVE\"}\n"<<std::flush;
            std::_Exit(3);
        }
        pass=!exists(f.id) && (f.foreign_bind || f.foreign_snapshot?f.foreign_actual_reentry:f.foreign_unlocked) &&
            f.lifetime_retains==1 && f.lifetime_releases==1;
        mgmem_domain_begin_close_v1(domain);mgmem_domain_collect_v1(domain);mgmem_domain_drain_v1(domain,0);
        if(mgmem_domain_destroy_v1(domain)!=MGMEM_OK)pass=false;
    } else {
        make_done(f,test_case=="error-quarantine"?MGMEM_PROVIDER_ERROR:MGMEM_OK);
        if(test_case=="callback-reentry") {
            publish_closed(f.id,f.state);
            pass=f.calls==1 && f.jni_unlocked && f.snapshot_reentered && f.global_deletes==2 && !exists(f.id);
        } else if(test_case=="single-publisher") {
            f.block_callback=true;
            std::thread first([&]{publish_closed(f.id,f.state);});
            {std::unique_lock<std::mutex> lock(f.gate);f.changed.wait_for(lock,std::chrono::seconds(2),[&]{return f.entered.load();});}
            std::thread second([&]{publish_closed(f.id,f.state,&env);});second.join();
            const bool snapshot_during_callback=JNI_NAME(nativeSnapshot)(&env,nullptr,static_cast<jlong>(f.id))!=nullptr;
            f.resume=true;f.changed.notify_all();first.join();publish_closed(f.id,f.state,&env);
            pass=f.entered && snapshot_during_callback && f.calls==1 && f.jni_unlocked && f.global_deletes==2 && !exists(f.id);
        } else if(test_case=="attach-retry") {
            f.detached=true;f.attach_fails=true;publish_closed(f.id,f.state);
            const bool retained=exists(f.id) && f.global_deletes==0 && f.calls==0 && f.state->listener;
            f.attach_fails=false;publish_closed(f.id,f.state);
            pass=retained && f.attach_calls==2 && f.detach_calls==1 && f.calls==1 && f.global_deletes==2 &&
                f.jni_unlocked && !exists(f.id);
        } else if(test_case=="java-exception-retry") {
            f.throw_once=true;publish_closed(f.id,f.state);
            const bool retained=exists(f.id) && f.global_deletes==0 && f.calls==1 && !pending_exception;
            publish_closed(f.id,f.state);
            pass=retained && f.calls==2 && f.global_deletes==2 && f.jni_unlocked && !exists(f.id);
        } else if(test_case=="error-quarantine") {
            publish_closed(f.id,f.state);publish_closed(f.id,f.state,&env);
            pass=f.calls==1 && f.global_deletes==0 && exists(f.id) && f.jni_unlocked;
        } else return 2;
    }
    std::cout<<"{\"case\":\""<<test_case<<"\",\"pass\":"<<(pass?"true":"false")
             <<",\"callbacks\":"<<f.calls<<",\"global_ref_deletes\":"<<f.global_deletes
             <<",\"JNI_operations_outside_Bridge\":"<<(f.jni_unlocked?"true":"false")
             <<",\"snapshot_reentered\":"<<(f.snapshot_reentered?"true":"false")
             <<",\"foreign_actual_JNI_reentry\":"<<(f.foreign_actual_reentry?"true":"false")
             <<",\"foreign_release_outside_Bridge\":"<<(f.foreign_unlocked?"true":"false")<<"}\n";
    return pass?0:1;
}
