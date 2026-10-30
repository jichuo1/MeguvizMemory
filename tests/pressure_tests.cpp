#include <mgmem/pressure.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <thread>
#if defined(_WIN32)
#include <malloc.h>
#endif
namespace {
unsigned checks=0;
#define CHECK(x) do {++checks;if(!(x)){std::fprintf(stderr,"FAIL %s:%d %s\n",__FILE__,__LINE__,#x);std::abort();}} while(false)
struct Lifetime {
    std::atomic<unsigned> retained{0},released{0};
    std::thread::id release_thread;
    static void retain(void *p) {++static_cast<Lifetime *>(p)->retained;}
    static void release(void *p) {auto &v=*static_cast<Lifetime *>(p);v.release_thread=std::this_thread::get_id();++v.released;}
    mgmem_domain_lifetime_v1 table(){return {sizeof(mgmem_domain_lifetime_v1),1,this,retain,release};}
};
mgmem_pressure_options_v1 policy(){return {sizeof(mgmem_pressure_options_v1),1,1024,512,0,5,0};}
mgmem_domain *create(const mgmem_provider_v1 *provider=nullptr) {
    mgmem_domain_options_v1 options{sizeof(options),MGMEM_ABI_MAJOR,MGMEM_ABI_MINOR,0,MGMEM_ARENA,0,1024,256,provider};
    mgmem_domain *d=nullptr;CHECK(mgmem_domain_create_v1(&options,&d)==MGMEM_OK);return d;
}
void cleanup(mgmem_domain *d) {
    CHECK(mgmem_domain_begin_close_v1(d)==MGMEM_OK);CHECK(mgmem_domain_collect_v1(d)==MGMEM_OK);
    CHECK(mgmem_domain_drain_v1(d,0)==MGMEM_OK);CHECK(mgmem_domain_destroy_v1(d)==MGMEM_OK);
}
mgmem_pressure_snapshot_v1 snapshot(mgmem_pressure_controller *p) {
    mgmem_pressure_snapshot_v1 s{};CHECK(mgmem_pressure_snapshot_v1_get(p,&s,sizeof(s))==MGMEM_OK);return s;
}
template<class Predicate>void eventually(Predicate predicate) {
    auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
    while(!predicate()){CHECK(std::chrono::steady_clock::now()<deadline);std::this_thread::sleep_for(std::chrono::milliseconds(2));}
}
void retiring_close() {
    auto *d=create();Lifetime life;auto t=life.table();auto options=policy();mgmem_pressure_controller *p=nullptr;
    CHECK(mgmem_pressure_create_v1(d,&t,&options,&p)==MGMEM_OK);CHECK(life.retained==1);
    mgmem_pressure_controller *duplicate=nullptr;CHECK(mgmem_pressure_create_v1(d,&t,&options,&duplicate)==MGMEM_BUSY);CHECK(!duplicate);
    CHECK(life.retained==2 && life.released==1);
    mgmem_handle_v1 owner{};CHECK(mgmem_allocate_v1(d,128,64,&owner)==MGMEM_OK);
    mgmem_borrow_v1 view{};CHECK(mgmem_borrow_acquire_v1(d,owner,0,128,MGMEM_WRITE,&view)==MGMEM_OK);
    std::memset(view.data,0x5a,128);CHECK(mgmem_release_v1(d,owner)==MGMEM_OK);
    CHECK(mgmem_pressure_notify_v1(p,MGMEM_PRESSURE_CRITICAL)==MGMEM_OK);
    eventually([&]{return snapshot(p).deferred_busy>0;});auto s=snapshot(p);
    CHECK(s.domain.budget_bytes==0 && s.domain.retiring_bytes==128 && s.domain.backing_bytes==256 && s.over_budget_bytes==256);
    mgmem_handle_v1 denied{};CHECK(mgmem_allocate_v1(d,1,1,&denied)==MGMEM_OUT_OF_BUDGET);
    CHECK(static_cast<unsigned char *>(view.data)[127]==0x5a);
    CHECK(mgmem_pressure_begin_close_v1(p)==MGMEM_OK);CHECK(mgmem_pressure_notify_v1(p,0)==MGMEM_IS_CLOSED);
    CHECK(mgmem_pressure_drain_v1(p,1000)==MGMEM_OK);CHECK(life.released==1);
    CHECK(mgmem_pressure_destroy_v1(p)==MGMEM_OK);CHECK(life.released==2 && life.release_thread==std::this_thread::get_id());
    CHECK(static_cast<unsigned char *>(view.data)[0]==0x5a);CHECK(mgmem_borrow_release_v1(d,view.lease)==MGMEM_OK);cleanup(d);
}
void deferred_trim_restore() {
    auto *d=create();Lifetime life;auto t=life.table();auto options=policy();mgmem_pressure_controller *p=nullptr;
    CHECK(mgmem_pressure_create_v1(d,&t,&options,&p)==MGMEM_OK);
    mgmem_handle_v1 owner{};CHECK(mgmem_allocate_v1(d,128,64,&owner)==MGMEM_OK);
    CHECK(mgmem_pressure_notify_v1(p,2)==MGMEM_OK);eventually([&]{return snapshot(p).deferred_busy>0;});
    CHECK(mgmem_release_v1(d,owner)==MGMEM_OK);eventually([&]{return snapshot(p).trim_successes>0;});
    CHECK(snapshot(p).domain.backing_bytes==0);CHECK(mgmem_pressure_notify_v1(p,0)==MGMEM_OK);
    CHECK(snapshot(p).domain.budget_bytes==1024);CHECK(mgmem_allocate_v1(d,200,64,&owner)==MGMEM_OK);
    CHECK(mgmem_release_v1(d,owner)==MGMEM_OK);CHECK(mgmem_pressure_begin_close_v1(p)==MGMEM_OK);
    CHECK(mgmem_pressure_drain_v1(p,1000)==MGMEM_OK);CHECK(mgmem_pressure_destroy_v1(p)==MGMEM_OK);cleanup(d);
}
struct SlowProvider {
    std::mutex mutex;std::condition_variable changed;bool entered=false,allow=false;std::atomic<unsigned> frees{0};std::thread::id callback;
    static mgmem_status allocate(void *,uint64_t bytes,uint64_t alignment,void **out) {
#if defined(_WIN32)
        *out=_aligned_malloc(static_cast<size_t>(bytes),static_cast<size_t>(alignment));
#else
        *out=nullptr;if(posix_memalign(out,static_cast<size_t>(alignment),static_cast<size_t>(bytes)))return MGMEM_OUT_OF_MEMORY;
#endif
        return *out?MGMEM_OK:MGMEM_OUT_OF_MEMORY;
    }
    static void free(void *p,void *address,uint64_t,uint64_t) {
        auto &s=*static_cast<SlowProvider *>(p);std::unique_lock<std::mutex> lock(s.mutex);s.callback=std::this_thread::get_id();s.entered=true;s.changed.notify_all();
        s.changed.wait(lock,[&]{return s.allow;});lock.unlock();
#if defined(_WIN32)
        _aligned_free(address);
#else
        std::free(address);
#endif
        ++s.frees;
    }
    static void retain(void *){} static void release(void *){}
};
void blocked_cleanup() {
    SlowProvider slow;mgmem_provider_v1 provider{sizeof(provider),MGMEM_ABI_MAJOR,MGMEM_ABI_MINOR,0,&slow,SlowProvider::allocate,SlowProvider::free,SlowProvider::retain,SlowProvider::release};
    auto *d=create(&provider);mgmem_handle_v1 owner{};CHECK(mgmem_allocate_v1(d,128,64,&owner)==MGMEM_OK);CHECK(mgmem_release_v1(d,owner)==MGMEM_OK);
    Lifetime life;auto t=life.table();auto options=policy();mgmem_pressure_controller *p=nullptr;CHECK(mgmem_pressure_create_v1(d,&t,&options,&p)==MGMEM_OK);
    CHECK(mgmem_pressure_notify_v1(p,2)==MGMEM_OK);
    {std::unique_lock<std::mutex> lock(slow.mutex);CHECK(slow.changed.wait_for(lock,std::chrono::seconds(3),[&]{return slow.entered;}));}
    CHECK(slow.callback!=std::this_thread::get_id());CHECK(mgmem_pressure_begin_close_v1(p)==MGMEM_OK);
    CHECK(mgmem_pressure_drain_v1(p,0)==MGMEM_TIMEOUT);CHECK(mgmem_pressure_drain_v1(p,5)==MGMEM_TIMEOUT);CHECK(mgmem_pressure_destroy_v1(p)==MGMEM_BUSY);
    CHECK(life.released==0 && slow.frees==0);
    {std::lock_guard<std::mutex> lock(slow.mutex);slow.allow=true;slow.changed.notify_all();}
    CHECK(mgmem_pressure_drain_v1(p,1000)==MGMEM_OK);CHECK(mgmem_pressure_destroy_v1(p)==MGMEM_OK);CHECK(slow.frees==1 && life.released==1);cleanup(d);
}
}
int main(){retiring_close();deferred_trim_restore();blocked_cleanup();std::printf("{\"suite\":\"mgmem-pressure-v1\",\"tests\":3,\"checks\":%u,\"status\":\"PASS\"}\n",checks);}
