#include "stream_trace.hpp"
#include "trace_sha256.hpp"
#include <mgmem/mgmem.h>
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <span>
#include <stdexcept>
#include <streambuf>
#include <string>

// Recorder test only; no real Memory or provider is linked.
extern "C" uint32_t mgmem_abi_version_v1(void) { return 0x00010001u; }

namespace {
int checks = 0;
void check(bool value) { ++checks; if (!value) throw std::runtime_error("stream_contract"); }
void limit(const char *value) {
#if defined(_WIN32)
    check(_putenv_s("MGMEM_DEX_TRACE_MAX_EVENTS", value) == 0);
#else
    check(setenv("MGMEM_DEX_TRACE_MAX_EVENTS", value, 1) == 0);
#endif
}
uint64_t read64(const std::string &s, size_t at) {
    check(at + 8 <= s.size()); uint64_t v = 0;
    for (unsigned i = 0; i != 8; ++i) v |= uint64_t(static_cast<unsigned char>(s[at + i])) << (8 * i);
    return v;
}
uint32_t read32(const std::string &s, size_t at) {
    check(at + 4 <= s.size()); uint32_t v = 0;
    for (unsigned i = 0; i != 4; ++i) v |= uint32_t(static_cast<unsigned char>(s[at + i])) << (8 * i);
    return v;
}
uint16_t read16(const std::string &s, size_t at) {
    check(at + 2 <= s.size()); return static_cast<uint16_t>(static_cast<unsigned char>(s[at]) |
        (uint16_t(static_cast<unsigned char>(s[at + 1])) << 8));
}
std::string reason(const mgmem_dex_trace_v2::Recorder &r) {
    std::ostringstream out; check(r.report_failure(out)); return out.str();
}
class FailingBuffer final : public std::streambuf {
public:
    explicit FailingBuffer(size_t budget) : budget_(budget) {}
protected:
    std::streamsize xsputn(const char *, std::streamsize count) override {
        const auto accepted = std::min<size_t>(budget_, static_cast<size_t>(count));
        budget_ -= accepted;
        return static_cast<std::streamsize>(accepted);
    }
    int overflow(int) override { return traits_type::eof(); }
private:
    size_t budget_;
};
}

int main() {
    try {
        uint8_t bytes[]{'d', 'e', 'x', '\n'}, foreign[]{'x', 'x', 'x', 'x'};
        std::span<const uint8_t> input(bytes), other(foreign);
        limit("3");
        {
            std::ostringstream out(std::ios::out | std::ios::binary);
            mgmem_dex_trace_v2::Recorder r(out);
            { mgmem_dex_trace_v2::Scope s(r, bytes, sizeof(bytes));
              r.record(input, 0, 1); r.record(input, 3, 1001); }
            check(r.complete()); check(r.finish());
            out << "MGMEM_CORE_MODULE_HEX 61\n{\"success\":true}\n"; out.flush();
            const auto data = out.str();
            check(data.substr(0, 8) == "MDXTRC2\n"); check(read64(data, 8) == 4); check(read64(data, 16) == 3);
            check(data.substr(24, 64) == image_trace::sha256_hex(bytes, sizeof(bytes)));
            check(static_cast<unsigned char>(data[88]) == 1 && read16(data, 89) == 1 && read32(data, 91) == 0);
            check(static_cast<unsigned char>(data[95]) == 1 && read16(data, 96) == 1001 && read32(data, 98) == 3);
            check(static_cast<unsigned char>(data[102]) == 2 && read64(data, 103) == 2);
            check(data.substr(111) == "MGMEM_CORE_MODULE_HEX 61\n{\"success\":true}\n");
            check(!r.finish());
        }
        {
            std::ostringstream out(std::ios::out | std::ios::binary);
            mgmem_dex_trace_v2::Recorder r(out); { mgmem_dex_trace_v2::Scope s(r, bytes, sizeof(bytes)); }
            check(r.finish()); const auto data = out.str();
            check(data.size() == 97 && static_cast<unsigned char>(data[88]) == 2 && read64(data, 89) == 0);
        }
        {
            limit("1"); std::ostringstream out(std::ios::out | std::ios::binary);
            mgmem_dex_trace_v2::Recorder r(out); { mgmem_dex_trace_v2::Scope s(r, bytes, sizeof(bytes));
                r.record(input, 0, 1); r.record(input, 1, 2); r.record(input, 2, 3); }
            check(!r.finish()); const auto msg = reason(r);
            check(msg.find("\"reason\":\"EVENT_LIMIT\"") != std::string::npos &&
                msg.find("\"attempted_lower_bound\":2") != std::string::npos &&
                msg.find("\"attempted_capped\":true") != std::string::npos &&
                msg.find("\"recorded\":1") != std::string::npos);
            check(out.str().find("MDXTRC2\n") == std::string::npos); // buffered partial output is not a footer
        }
        {
            limit("3"); std::ostringstream out(std::ios::out | std::ios::binary);
            mgmem_dex_trace_v2::Recorder r(out); { mgmem_dex_trace_v2::Scope s(r, bytes, sizeof(bytes)); r.record(other, 0, 1); }
            check(!r.finish() && reason(r).find("SPAN_MISMATCH") != std::string::npos);
        }
        {
            std::ostringstream out(std::ios::out | std::ios::binary);
            mgmem_dex_trace_v2::Recorder r(out); { mgmem_dex_trace_v2::Scope s(r, bytes, sizeof(bytes)); r.record(input, 4, 1); }
            check(!r.finish() && reason(r).find("OFFSET_OUT_OF_RANGE") != std::string::npos);
        }
        {
            std::ostringstream out(std::ios::out | std::ios::binary);
            mgmem_dex_trace_v2::Recorder r(out); { mgmem_dex_trace_v2::Scope s(r, bytes, sizeof(bytes)); r.record(input, 0, 32); }
            check(!r.finish() && reason(r).find("SITE_INVALID") != std::string::npos);
        }
        {
            FailingBuffer buffer(4); std::ostream out(&buffer);
            mgmem_dex_trace_v2::Recorder r(out); { mgmem_dex_trace_v2::Scope s(r, bytes, sizeof(bytes)); r.record(input, 0, 1); }
            check(!r.finish() && reason(r).find("OUTPUT_FAILURE") != std::string::npos);
        }
        {
            limit("64000001"); bool rejected = false;
            try { std::ostringstream out; mgmem_dex_trace_v2::Recorder r(out); }
            catch (const std::runtime_error &) { rejected = true; }
            check(rejected);
        }
        {
            // Cross the 64 KiB buffer boundary without changing event order.
            limit("10000"); std::ostringstream out(std::ios::out | std::ios::binary);
            mgmem_dex_trace_v2::Recorder r(out); { mgmem_dex_trace_v2::Scope s(r, bytes, sizeof(bytes));
                for (unsigned i = 0; i != 10000; ++i) r.record(input, i % 4, i % 2 ? 1001 : 1); }
            check(r.finish()); const auto data = out.str();
            check(data.size() == 88 + 10000 * 7 + 9);
            for (unsigned i : {0u, 9348u, 9349u, 9999u}) {
                const auto at = 88 + i * 7;
                check(static_cast<unsigned char>(data[at]) == 1 && read16(data, at + 1) == (i % 2 ? 1001 : 1) &&
                      read32(data, at + 3) == i % 4);
            }
            const auto trailer = 88 + 10000 * 7;
            check(static_cast<unsigned char>(data[trailer]) == 2 && read64(data, trailer + 1) == 10000);
        }
        std::cout << "PASS V2 streaming recorder " << checks << " checks\n";
        return 0;
    } catch (...) { return 1; }
}
