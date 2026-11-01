#include "consumer_trace.hpp"
#include <mgmem/mgmem.h>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <span>
#include <stdexcept>
#include <string>

// Recorder-only test: no Memory runtime or provider is needed.
extern "C" uint32_t mgmem_abi_version_v1(void) { return 0x00010001u; }

namespace {
int checks = 0;
void check(bool value) { ++checks; if (!value) throw std::runtime_error("recorder_contract"); }
void limit(const char *value) {
#if defined(_WIN32)
    check(_putenv_s("MGMEM_DEX_TRACE_MAX_EVENTS", value) == 0);
#else
    check(setenv("MGMEM_DEX_TRACE_MAX_EVENTS", value, 1) == 0);
#endif
}
std::string failure(const mgmem_dex_trace::Recorder &recorder) {
    std::ostringstream output;
    check(recorder.report_failure(output));
    return output.str();
}
void expect(const std::string &output, const std::string &reason, int attempted, bool capped, int recorded, int max) {
    const auto expected = "MGMEM_DEX_TRACE_INCOMPLETE {\"reason\":\"" + reason +
        "\",\"attempted_lower_bound\":" + std::to_string(attempted) +
        ",\"attempted_capped\":" + (capped ? "true" : "false") +
        ",\"recorded\":" + std::to_string(recorded) + ",\"event_limit\":" + std::to_string(max) + "}\n";
    check(output == expected);
}
}

int main() {
    try {
        uint8_t bytes[]{'d', 'e', 'x', '\n'}, other[]{'x', 'x', 'x', 'x'};
        const std::span<const uint8_t> input(bytes), foreign(other);
        limit("2");
        {
            mgmem_dex_trace::Recorder r; r.enter(bytes, sizeof(bytes));
            r.record(input, 0, 1); r.record(input, 1, 2); r.record(input, 2, 3); r.leave();
            check(!r.complete()); expect(failure(r), "EVENT_LIMIT", 3, false, 2, 2);
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(bytes, sizeof(bytes));
            for (unsigned i = 0; i != 7; ++i) r.record(input, 0, 1);
            r.leave(); check(!r.complete()); expect(failure(r), "EVENT_LIMIT", 3, true, 2, 2);
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(bytes, sizeof(bytes)); r.record(foreign, 0, 1); r.leave();
            expect(failure(r), "SPAN_MISMATCH", 1, false, 0, 2);
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(bytes, sizeof(bytes)); r.record(input, sizeof(bytes), 1); r.leave();
            expect(failure(r), "OFFSET_OUT_OF_RANGE", 1, false, 0, 2);
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(bytes, sizeof(bytes)); r.record(input, 0, 32); r.leave();
            expect(failure(r), "SITE_INVALID", 1, false, 0, 2);
        }
        {
            mgmem_dex_trace::Recorder r; r.record(input, 0, 1);
            expect(failure(r), "SCOPE_INVALID", 1, false, 0, 2);
        }
        {
            mgmem_dex_trace::Recorder outer, nested; outer.enter(bytes, sizeof(bytes));
            bool rejected = false;
            try { nested.enter(bytes, sizeof(bytes)); } catch (const std::runtime_error &) { rejected = true; }
            check(rejected); expect(failure(nested), "SCOPE_INVALID", 0, false, 0, 2); outer.leave();
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(bytes, sizeof(bytes)); r.record(input, 0, 1); r.leave();
            check(r.complete()); std::ostringstream output; output.setstate(std::ios::badbit);
            check(!r.emit(output)); expect(failure(r), "OUTPUT_FAILURE", 1, false, 1, 2);
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(bytes, sizeof(bytes));
            r.record(foreign, 0, 1); r.record(input, 0, 1); r.record(input, 1, 1); r.record(input, 2, 1);
            r.leave(); expect(failure(r), "SPAN_MISMATCH", 3, true, 2, 2);
        }
        {
            mgmem_dex_trace::Recorder r; r.enter(bytes, sizeof(bytes)); r.record(input, 0, 1); r.leave();
            std::ostringstream output; check(r.emit(output));
            check(output.str().find("MGMEM_DEX_READER_TRACE_V1 1 ") == 0);
            check(output.str().find("E 1 0\n") != std::string::npos);
        }
        std::cout << "PASS fixed recorder categories " << checks << " checks\n";
        return 0;
    } catch (...) { std::cerr << "FAIL recorder contract\n"; return 1; }
}
