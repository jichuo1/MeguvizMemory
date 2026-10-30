#include <mgmem/mgmem.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <future>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#if defined(_WIN32)
#include <malloc.h>
#endif
#if defined(__linux__)
#include <sched.h>
#endif

namespace {
using u64 = uint64_t;
void require(bool condition, const char *message) { if (!condition) throw std::runtime_error(message); }
void check(mgmem_status status) {
    if (status != MGMEM_OK) throw std::runtime_error("Memory status " + std::to_string(status));
}
u64 number(const std::string &text) {
    require(!text.empty(), "empty number");
    u64 value = 0;
    for (char c : text) {
        require(c >= '0' && c <= '9', "invalid number");
        const unsigned digit = static_cast<unsigned>(c - '0');
        require(value <= (UINT64_MAX - digit) / 10, "number overflow");
        value = value * 10 + digit;
    }
    return value;
}
u64 multiply(u64 a, u64 b) { require(!b || a <= UINT64_MAX / b, "size overflow"); return a*b; }
std::string json_string(const std::string &value) {
    std::string result = "\"";
    for (unsigned char c : value) {
        if (c == '\\' || c == '"') { result += '\\'; result += static_cast<char>(c); }
        else if (c >= 32 && c < 127) result += static_cast<char>(c);
        else result += '?';
    }
    return result + '"';
}
// Scalar SHA-256 for file identity only; all hashing is before the timed task.
// SHA constants and operations are the public algorithm, not a new dependency.
uint32_t rotate(uint32_t x, unsigned n) { return (x >> n) | (x << (32-n)); }
std::string sha256(const std::vector<uint8_t> &data) {
    constexpr uint32_t k[64] = {
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
    std::array<uint32_t,8> h{0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    auto block = [&](const uint8_t *p) {
        std::array<uint32_t,64> w{};
        for (size_t i=0; i<16; ++i) w[i]=(uint32_t(p[4*i])<<24)|(uint32_t(p[4*i+1])<<16)|(uint32_t(p[4*i+2])<<8)|p[4*i+3];
        for (size_t i=16; i<64; ++i) {
            const uint32_t a=rotate(w[i-15],7)^rotate(w[i-15],18)^(w[i-15]>>3);
            const uint32_t b=rotate(w[i-2],17)^rotate(w[i-2],19)^(w[i-2]>>10);
            w[i]=w[i-16]+a+w[i-7]+b;
        }
        uint32_t a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],v=h[7];
        for (size_t i=0; i<64; ++i) {
            const uint32_t s1=rotate(e,6)^rotate(e,11)^rotate(e,25);
            const uint32_t t1=v+s1+((e&f)^((~e)&g))+k[i]+w[i];
            const uint32_t s0=rotate(a,2)^rotate(a,13)^rotate(a,22);
            const uint32_t t2=s0+((a&b)^(a&c)^(b&c));
            v=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=v;
    };
    size_t offset=0;
    while (data.size()-offset>=64) { block(data.data()+offset); offset+=64; }
    std::array<uint8_t,128> tail{};
    const size_t left=data.size()-offset, padded=left<56?64:128;
    std::copy(data.begin()+static_cast<ptrdiff_t>(offset),data.end(),tail.begin());
    tail[left]=0x80;
    const u64 bits=static_cast<u64>(data.size())*8;
    for (size_t i=0;i<8;++i) tail[padded-1-i]=static_cast<uint8_t>(bits>>(8*i));
    block(tail.data()); if (padded==128) block(tail.data()+64);
    constexpr char hex[]="0123456789abcdef";
    std::string result; result.reserve(64);
    for (uint32_t word:h) for (int i=7;i>=0;--i) result+=hex[(word>>(4*i))&15];
    return result;
}
bool whitespace(uint8_t c) { return c==' ' || c=='\t' || c=='\r' || c=='\n'; }
std::string token(const std::vector<uint8_t> &bytes, size_t &offset) {
    while (offset<bytes.size()) {
        if (bytes[offset]=='#') { while(offset<bytes.size() && bytes[offset]!='\n') ++offset; }
        else if (whitespace(bytes[offset])) ++offset;
        else break;
    }
    std::string result;
    while(offset<bytes.size() && !whitespace(bytes[offset])) {
        require(result.size()<32,"oversized PPM token"); result+=static_cast<char>(bytes[offset++]);
    }
    require(!result.empty() && offset<bytes.size(),"truncated PPM header");
    const uint8_t delimiter=bytes[offset++];
    if (delimiter=='\r' && offset<bytes.size() && bytes[offset]=='\n') ++offset;
    return result;
}
void select_cpu(int cpu) {
    if (cpu<0) return;
#if defined(__linux__)
    require(cpu<CPU_SETSIZE,"affinity_cpu_out_of_range");
    cpu_set_t mask; CPU_ZERO(&mask); CPU_SET(cpu,&mask);
    require(sched_setaffinity(0,sizeof(mask),&mask)==0,"affinity_set_failed");
    CPU_ZERO(&mask);
    require(sched_getaffinity(0,sizeof(mask),&mask)==0 && CPU_COUNT(&mask)==1 && CPU_ISSET(cpu,&mask),"affinity_not_exact");
#else
    throw std::runtime_error("affinity_unsupported");
#endif
}
int current_cpu() {
#if defined(__linux__)
    return sched_getcpu();
#else
    return -1;
#endif
}
struct Workspace {
    std::string provider;
    std::array<u64,3> sizes{};
    std::array<void *,3> pointers{};
    std::array<mgmem_handle_v1,3> owners{};
    std::array<mgmem_borrow_v1,3> leases{};
    mgmem_domain *domain=nullptr;
    mgmem_snapshot_v1 initial{}, before_cleanup{}, final{};
    u64 direct_allocations=0, direct_backing=0, peak=0;
    bool closed=false, cleanup_ok=true;
    ~Workspace() { if(!closed) { try { finish(); } catch(...) {} } }
    void open(const std::string &kind,const std::array<u64,3> &lengths) {
        provider=kind; sizes=lengths;
        if(provider!="direct") {
            mgmem_domain_options_v1 options{sizeof(options),1,0,0,provider=="arena"?MGMEM_ARENA:MGMEM_SYSTEM,0,
                                             64ull*1024*1024,65536,nullptr};
            check(mgmem_domain_create_v1(&options,&domain));
        }
        for(size_t i=0;i<3;++i) {
            if(domain) check(mgmem_allocate_v1(domain,sizes[i],64,&owners[i]));
            else {
#if defined(_WIN32)
                pointers[i]=_aligned_malloc(static_cast<size_t>(sizes[i]),64);
#else
                void *p=nullptr; if(posix_memalign(&p,64,static_cast<size_t>(sizes[i]))!=0) p=nullptr;
                pointers[i]=p;
#endif
                require(pointers[i]!=nullptr,"direct System allocation failed");
                direct_backing+=sizes[i]; ++direct_allocations;
            }
        }
        if(domain) { check(mgmem_domain_snapshot_v1(domain,&initial,sizeof(initial))); peak=initial.peak_backing_bytes; }
        else peak=direct_backing;
    }
    void acquire() {
        if(!domain) return;
        for(size_t i=0;i<3;++i) {
            check(mgmem_borrow_acquire_v1(domain,owners[i],0,sizes[i],MGMEM_WRITE,&leases[i]));
            pointers[i]=leases[i].data;
        }
    }
    void release_leases() {
        if(!domain) return;
        for(auto &lease:leases) if(lease.lease.id) { check(mgmem_borrow_release_v1(domain,lease.lease)); lease={}; }
        pointers={};
    }
    void finish() {
        if(closed) return;
        if(domain) {
            release_leases();
            check(mgmem_domain_snapshot_v1(domain,&before_cleanup,sizeof(before_cleanup)));
            for(auto &owner:owners) if(owner.id) { check(mgmem_release_v1(domain,owner)); owner={}; }
            check(mgmem_domain_begin_close_v1(domain)); check(mgmem_domain_collect_v1(domain));
            check(mgmem_domain_drain_v1(domain,0)); check(mgmem_domain_snapshot_v1(domain,&final,sizeof(final)));
            require(final.state==MGMEM_CLOSED && final.backing_bytes==0 && final.reserved_bytes==0 &&
                    final.active_allocations==0 && final.active_leases==0,"final_resources_not_zero");
            check(mgmem_domain_destroy_v1(domain)); domain=nullptr;
        } else {
            for(auto &p:pointers) if(p) {
#if defined(_WIN32)
                _aligned_free(p);
#else
                std::free(p);
#endif
                p=nullptr;
            }
            direct_backing=0;
        }
        closed=true;
    }
};
void gray_row(const uint8_t *rgb,uint8_t *temporary,u64 row,u64 width,u64 &converted) {
    for(u64 x=0;x<width;++x) {
        const u64 at=3*(row*width+x);
        temporary[x]=static_cast<uint8_t>((77u*rgb[at]+150u*rgb[at+1]+29u*rgb[at+2])>>8);
        ++converted;
    }
}
struct Output { u64 sum=0, hash=UINT64_C(14695981039346656037); };
Output process(const uint8_t *rgb,uint8_t *temporary,uint8_t *output,u64 width,u64 height,bool ring,u64 &converted) {
    const u64 rows=std::min<u64>(height,3);
    if(!ring) for(u64 y=0;y<height;++y) gray_row(rgb,temporary+y*width,y,width,converted);
    u64 next=0;
    Output result;
    for(u64 y=0;y<height;++y) {
        if(ring) while(next<=std::min(height-1,y+1)) { gray_row(rgb,temporary+(next%rows)*width,next,width,converted); ++next; }
        for(u64 x=0;x<width;++x) {
            unsigned sum=0;
            for(int dy=-1;dy<=1;++dy) {
                const u64 yy=dy<0?(y?y-1:0):(dy>0?std::min(y+1,height-1):y);
                const uint8_t *line=temporary+(ring?yy%rows:yy)*width;
                for(int dx=-1;dx<=1;++dx) {
                    const u64 xx=dx<0?(x?x-1:0):(dx>0?std::min(x+1,width-1):x);
                    sum+=line[xx];
                }
            }
            const uint8_t pixel=static_cast<uint8_t>(sum/9);
            output[y*width+x]=pixel;
            result.sum+=pixel; result.hash=(result.hash^pixel)*UINT64_C(1099511628211);
        }
    }
    return result;
}
std::string variant(const std::string &provider,const std::string &layout) {
    return std::string("I-")+(provider=="direct"?"D":provider=="system"?"S":"A")+(layout=="full"?"F":"R");
}
} // namespace

int main(int argc,char **argv) {
    Workspace workspace;
    std::vector<uint8_t> source, verification;
    std::string provider="direct",layout="ring",output_path,failure,source_hash;
    u64 frames=3,width=0,height=0,pixels=0,rgb_bytes=0,converted=0,completed=0,sum=0;
    u64 hash=UINT64_C(14695981039346656037),file_bytes=0,capacity=0,payload=0,total_ns=0,initial_calls=0;
    int cpu=-1,cpu_before=-1,cpu_after=-1;
    bool started=false,fault=false,source_released=false;
    std::chrono::steady_clock::time_point start{};
    try {
        require(argc>=2,"usage: mgmem_image_perf input.ppm --provider direct|system|arena --layout full|ring --frames 1..32 [--cpu N] [--verify-output file.pgm] [--fault-worker]");
        for(int i=2;i<argc;++i) {
            const std::string option=argv[i];
            if(option=="--fault-worker") fault=true;
            else {
                require(i+1<argc,"missing option value"); const std::string value=argv[++i];
                if(option=="--provider") provider=value;
                else if(option=="--layout") layout=value;
                else if(option=="--frames") frames=number(value);
                else if(option=="--cpu") { const u64 selected=number(value); require(selected<=65535,"cpu out of range"); cpu=static_cast<int>(selected); }
                else if(option=="--verify-output") output_path=value;
                else throw std::runtime_error("unknown option");
            }
        }
        require(provider=="direct" || provider=="system" || provider=="arena","invalid provider");
        require(layout=="full" || layout=="ring","invalid layout");
        require(frames>=1 && frames<=32,"frames outside bounded batch");
        select_cpu(cpu);
        std::ifstream file(argv[1],std::ios::binary|std::ios::ate);
        require(file.is_open(),"cannot open input"); const auto length=file.tellg();
        require(length>0 && static_cast<u64>(length)<=3ull*8*1024*1024+4096,"input outside bounded size");
        file_bytes=static_cast<u64>(length); source.resize(static_cast<size_t>(file_bytes));
        file.seekg(0); file.read(reinterpret_cast<char *>(source.data()),length);
        require(static_cast<u64>(file.gcount())==file_bytes,"truncated file read"); file.close();
        size_t offset=0;
        require(token(source,offset)=="P6","expected P6 PPM"); width=number(token(source,offset)); height=number(token(source,offset));
        require(width && height && number(token(source,offset))==255,"expected nonempty RGB8");
        pixels=multiply(width,height); rgb_bytes=multiply(pixels,3);
        require(pixels<=8*1024*1024 && offset<=source.size() && source.size()-offset==rgb_bytes,"truncated input or trailing bytes");
        capacity=source.capacity(); source_hash=sha256(source);
        const std::array<u64,3> sizes{rgb_bytes,layout=="ring"?multiply(std::min<u64>(height,3),width):pixels,pixels};
        payload=sizes[0]+sizes[1]+sizes[2];
        cpu_before=current_cpu(); start=std::chrono::steady_clock::now(); started=true;
        workspace.open(provider,sizes); initial_calls=workspace.domain?workspace.initial.provider_allocation_count:workspace.direct_allocations;
        for(u64 frame=0;frame<frames;++frame) {
            workspace.acquire();
            std::packaged_task<Output()> operation([&] {
                try {
                    std::memcpy(workspace.pointers[0],source.data()+offset,static_cast<size_t>(rgb_bytes));
                    if(fault) throw std::runtime_error("injected worker exception");
                    u64 count=0;
                    Output result=process(static_cast<const uint8_t *>(workspace.pointers[0]),static_cast<uint8_t *>(workspace.pointers[1]),
                                          static_cast<uint8_t *>(workspace.pointers[2]),width,height,layout=="ring",count);
                    require(count==pixels,"gray conversion count differs"); converted+=count;
                    if(!output_path.empty() && frame+1==frames) {
                        const auto *first=static_cast<const uint8_t *>(workspace.pointers[2]);
                        verification.assign(first,first+static_cast<size_t>(pixels));
                    }
                    workspace.release_leases(); return result;
                } catch(...) { workspace.release_leases(); throw; }
            });
            auto future=operation.get_future(); std::thread worker(std::move(operation)); worker.join();
            const Output result=future.get(); sum+=result.sum;
            // Mix each frame's complete-output digest, preserving batch order.
            for(unsigned byte=0;byte<8;++byte) hash=(hash^((result.hash>>(8*byte))&255))*UINT64_C(1099511628211);
            ++completed;
        }
        workspace.finish(); std::vector<uint8_t>().swap(source); source_released=true;
        total_ns=static_cast<u64>(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count());
        cpu_after=current_cpu();
        if(!output_path.empty()) {
            std::ofstream output(output_path,std::ios::binary); require(output.is_open(),"cannot open verification output");
            output<<"P5\n"<<width<<' '<<height<<"\n255\n";
            output.write(reinterpret_cast<const char *>(verification.data()),static_cast<std::streamsize>(verification.size()));
            output.close(); require(output.good(),"verification output write failed");
        }
    } catch(const std::exception &error) {
        failure=error.what();
        try { workspace.finish(); } catch(const std::exception &cleanup) { workspace.cleanup_ok=false; failure+="; cleanup: "; failure+=cleanup.what(); }
        std::vector<uint8_t>().swap(source); source_released=true;
        if(started) total_ns=static_cast<u64>(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count());
        cpu_after=current_cpu();
    }
    const bool success=failure.empty(), diagnostic=fault || !output_path.empty();
    const u64 calls=provider=="direct"?workspace.direct_allocations:workspace.before_cleanup.provider_allocation_count;
    std::cout<<"{\"schema_version\":2,\"workload\":\"image_workspace_complete_owner_v2\",\"case\":\""<<(fault?"worker-failure":"normal")
        <<"\",\"success\":"<<(success?"true":"false")<<",\"variant\":"<<json_string(variant(provider,layout))
        <<",\"provider\":"<<json_string(provider)<<",\"layout\":"<<json_string(layout)<<",\"diagnostic\":"<<(diagnostic?"true":"false")
        <<",\"phases\":null,\"source_sha256\":"<<json_string(source_hash)<<",\"input_bytes\":"<<file_bytes
        <<",\"width\":"<<width<<",\"height\":"<<height<<",\"frames\":"<<frames<<",\"completed_frames\":"<<completed
        <<",\"result_checksum\":"<<sum<<",\"output_hash_fnv64\":"<<hash<<",\"gray_conversions\":"<<converted<<",\"total_ns\":"<<total_ns
        <<",\"logical_requested_bytes\":"<<payload<<",\"unique_owned_backing_peak_bytes\":"<<workspace.peak
        <<",\"external_source_capacity_bytes\":"<<capacity<<",\"provider_allocation_count\":"<<calls
        <<",\"initial_provider_allocation_count\":"<<initial_calls<<",\"steady_state_provider_allocations\":";
    if(workspace.closed && calls>=initial_calls) std::cout<<calls-initial_calls; else std::cout<<"null";
    std::cout<<",\"retained_before_collect_bytes\":"<<workspace.before_cleanup.retained_bytes
        <<",\"arena_chunk_bytes\":"<<(provider=="arena"?65536:0)<<",\"reserved_final_bytes\":"<<workspace.final.reserved_bytes
        <<",\"arena_used_bytes\":null,\"input_backing_padding_bytes\":null,\"memory_metadata_bytes\":null,\"rss_pss_bytes\":null,\"energy\":null"
        <<",\"source_final_released\":"<<(source_released?"true":"false")<<",\"lease_final_correct\":"<<(workspace.closed && workspace.cleanup_ok?"true":"false")
        <<",\"closed_backing_bytes\":"<<(workspace.domain?workspace.final.backing_bytes:workspace.direct_backing)
        <<",\"final_resource_state\":"<<json_string(workspace.closed && workspace.cleanup_ok?"CLOSED_ZERO":"UNKNOWN")
        <<",\"cpu_requested\":"<<cpu<<",\"cpu_before\":"<<cpu_before<<",\"cpu_after\":"<<cpu_after<<",\"failure_reason\":"<<json_string(failure)<<"}\n";
    return success?0:1;
}
