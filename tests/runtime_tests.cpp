#include <mgmem/mgmem.h>
#include <cstdio>
#include <cstdlib>
#if defined(_WIN32)
#include <windows.h>
using Module = HMODULE;
Module open(const char *path) { return LoadLibraryA(path); }
void *symbol(Module m, const char *name) { return reinterpret_cast<void *>(GetProcAddress(m, name)); }
void close(Module m) { FreeLibrary(m); }
#else
#include <dlfcn.h>
using Module = void *;
Module open(const char *path) { return dlopen(path, RTLD_NOW | RTLD_LOCAL); }
void *symbol(Module m, const char *name) { return dlsym(m, name); }
void close(Module m) { dlclose(m); }
#endif
#define CHECK(value) do { if (!(value)) { std::fprintf(stderr,"runtime check line %d\n",__LINE__); std::abort(); } } while(0)
struct Api {
    Module module;
    decltype(&mgmem_domain_create_v1) create;
    decltype(&mgmem_allocate_v1) allocate;
    decltype(&mgmem_release_v1) release;
    decltype(&mgmem_domain_begin_close_v1) begin;
    decltype(&mgmem_domain_collect_v1) collect;
    decltype(&mgmem_domain_destroy_v1) destroy;
    decltype(&mgmem_import_external_v1) import_external;
    decltype(&mgmem_runtime_origin_v1) origin;
    explicit Api(const char *path) : module(open(path)) {
        CHECK(module);
#define LOAD(field, name) field = reinterpret_cast<decltype(field)>(symbol(module, name)); CHECK(field)
        LOAD(create,"mgmem_domain_create_v1"); LOAD(allocate,"mgmem_allocate_v1");
        LOAD(release,"mgmem_release_v1"); LOAD(begin,"mgmem_domain_begin_close_v1");
        LOAD(collect,"mgmem_domain_collect_v1"); LOAD(destroy,"mgmem_domain_destroy_v1");
        LOAD(import_external,"mgmem_import_external_v1"); LOAD(origin,"mgmem_runtime_origin_v1");
#undef LOAD
    }
    ~Api() { close(module); }
};
int main(int argc, char **argv) {
    CHECK(argc == 3);
    Api a(argv[1]), b(argv[2]);
    mgmem_domain_options_v1 options{sizeof(options),1,0,0,MGMEM_SYSTEM,0,4096,0,nullptr};
    mgmem_domain *da=nullptr,*db=nullptr;
    mgmem_handle_v1 ha{},hb{};
    CHECK(a.create(&options,&da)==MGMEM_OK && b.create(&options,&db)==MGMEM_OK);
    CHECK(a.allocate(da,64,16,&ha)==MGMEM_OK && b.allocate(db,64,16,&hb)==MGMEM_OK);
    CHECK(ha.domain_id==hb.domain_id && ha.id==hb.id && ha.runtime_origin!=hb.runtime_origin);
    CHECK(a.release(da,hb)==MGMEM_STALE_HANDLE && b.release(db,ha)==MGMEM_STALE_HANDLE);
    CHECK(a.release(da,ha)==MGMEM_OK && b.release(db,hb)==MGMEM_OK);
    alignas(64) unsigned char external[128]{};
    uint64_t callbacks[2]{};
    mgmem_external_buffer_v1 descriptor{};
    descriptor.struct_size=sizeof(descriptor);
    descriptor.abi_major=MGMEM_ABI_MAJOR; descriptor.abi_minor=MGMEM_ABI_MINOR;
    descriptor.data=external; descriptor.capacity_bytes=sizeof(external);
    descriptor.alignment=64; descriptor.external_id=1; descriptor.access=MGMEM_READ;
    descriptor.context=callbacks;
    descriptor.retain=[](void *context)->mgmem_status { ++static_cast<uint64_t*>(context)[0];return MGMEM_OK; };
    descriptor.release=[](void *context) { ++static_cast<uint64_t*>(context)[1]; };
    descriptor.managing_runtime_origin=a.origin();
    mgmem_handle_v1 imported{},foreign{};
    CHECK(a.import_external(da,&descriptor,&imported)==MGMEM_OK);
    // The very same bound descriptor is rejected before retaining its owner in
    // another genuinely loaded runtime. Re-tagging its backing is unsupported.
    CHECK(b.import_external(db,&descriptor,&foreign)==MGMEM_STALE_HANDLE);
    CHECK(foreign.id==0 && callbacks[0]==1 && callbacks[1]==0);
    CHECK(a.release(da,imported)==MGMEM_OK && callbacks[1]==1);
    CHECK(a.begin(da)==MGMEM_OK && b.begin(db)==MGMEM_OK);
    CHECK(a.collect(da)==MGMEM_OK && b.collect(db)==MGMEM_OK);
    CHECK(a.destroy(da)==MGMEM_OK && b.destroy(db)==MGMEM_OK);
    std::puts("{\"separate_runtime_handles_rejected\":true,\"foreign_runtime_import_descriptor_rejected\":true}");
}
