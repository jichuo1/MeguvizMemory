#include "consumer_trace.hpp"
#include <mgmem/mgmem.h>
#include <cstdint>
#include <iostream>
#include <limits>
#include <sstream>
#include <span>
#include <stdexcept>
#include <string>

extern "C" uint32_t mgmem_abi_version_v1(void) { return 0x00010001u; }
namespace {
int checks = 0;
void check(bool v) { ++checks; if (!v) throw std::runtime_error("count_contract"); }
}
int main() {
    try {
        uint8_t data[] = {'d', 'e', 'x', '\n'}, other[] = {'x', 'x', 'x', 'x'};
        const std::span<const uint8_t> input(data), foreign(other);
        {
            mgmem_dex_trace::Recorder r;
            r.enter(data, sizeof(data));
            r.record(input, 0, 1); r.record(input, 1, 31); r.record(input, 1, 1001);
            r.leave(); std::ostringstream out;
            check(r.complete()); check(r.emit(out));
            check(out.str().find("MGMEM_DEX_READER_COUNT_V1 3 3221225473 ") == 0);
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(data, sizeof(data)); r.leave();
            std::ostringstream out; check(r.complete()); check(r.emit(out));
            check(out.str().find("MGMEM_DEX_READER_COUNT_V1 0 0 ") == 0);
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(data, sizeof(data)); r.record(foreign, 0, 1); r.leave();
            std::ostringstream out; check(!r.complete()); check(!r.emit(out)); check(out.str().empty());
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(data, sizeof(data)); r.record(input, sizeof(data), 1); r.leave();
            check(!r.complete());
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(data, sizeof(data)); r.record(input, 0, 32); r.leave();
            check(!r.complete());
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(data, sizeof(data));
            r.test_set_count(std::numeric_limits<uint64_t>::max()); r.record(input, 0, 1); r.leave();
            std::ostringstream out; check(!r.complete()); check(!r.emit(out)); check(out.str().empty());
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(data, sizeof(data));
            for (unsigned i = 0; i < 1000001; ++i) r.record(input, 0, 1);
            r.leave(); std::ostringstream out;
            check(r.complete()); check(r.emit(out));
            check(out.str().find("MGMEM_DEX_READER_COUNT_V1 1000001 1 ") == 0);
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(data, sizeof(data)); r.record(input, 0, 1); r.leave();
            std::ostringstream out; out.setstate(std::ios::badbit);
            check(!r.emit(out)); check(!r.complete());
        }
        {
            mgmem_dex_trace::Recorder outer, inner; outer.enter(data, sizeof(data));
            bool rejected = false; try { inner.enter(data, sizeof(data)); } catch (...) { rejected = true; }
            outer.leave(); check(rejected); check(!inner.complete()); check(outer.complete());
        }
        std::cout << "PASS scoped count recorder " << checks << " checks\n";
        return 0;
    } catch (...) { return 1; }
}
