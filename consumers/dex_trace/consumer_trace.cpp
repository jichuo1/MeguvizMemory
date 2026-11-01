#include "consumer_trace.hpp"
#include "trace_runtime.hpp"
#include "trace_sha256.hpp"
#include <mgmem/mgmem.h>
#include <cstdlib>
#include <ostream>
#include <stdexcept>
#if defined(_WIN32)
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace {
thread_local mgmem_dex_trace::Recorder *active = nullptr;
size_t event_limit() {
    const char *text = std::getenv("MGMEM_DEX_TRACE_MAX_EVENTS");
    if (!text) return 250000;
    if (!*text) throw std::runtime_error("trace_limit_invalid");
    size_t value = 0;
    for (const char *p = text; *p; ++p) {
        if (*p < '0' || *p > '9' || value > (1000000u - unsigned(*p - '0')) / 10u)
            throw std::runtime_error("trace_limit_invalid");
        value = value * 10u + unsigned(*p - '0');
    }
    if (!value || value > 1000000) throw std::runtime_error("trace_limit_invalid");
    return value;
}
}

namespace mgmem_dex_trace {
Recorder::Recorder() : limit_(event_limit()) { events_.reserve(limit_); }
void Recorder::fail(Failure reason) noexcept {
    if (failure_ == Failure::NONE) failure_ = reason;
}
const char *Recorder::failure_name() const noexcept {
    switch (failure_) {
    case Failure::EVENT_LIMIT: return "EVENT_LIMIT";
    case Failure::SPAN_MISMATCH: return "SPAN_MISMATCH";
    case Failure::OFFSET_OUT_OF_RANGE: return "OFFSET_OUT_OF_RANGE";
    case Failure::SITE_INVALID: return "SITE_INVALID";
    case Failure::OUTPUT_FAILURE: return "OUTPUT_FAILURE";
    case Failure::NONE:
    case Failure::SCOPE_INVALID: return "SCOPE_INVALID";
    }
    return "SCOPE_INVALID";
}
void Recorder::enter(const void *base, uint64_t size) {
    if (active || entered_ || !base || !size) {
        fail(Failure::SCOPE_INVALID); throw std::runtime_error("trace_scope_invalid");
    }
    try {
        base_ = static_cast<const uint8_t *>(base); size_ = size;
        input_sha_ = image_trace::sha256_hex(base_, static_cast<size_t>(size_));
        entered_ = true; active = this;
    } catch (...) { fail(Failure::SCOPE_INVALID); throw; }
}
void Recorder::leave() noexcept {
    if (active != this || !entered_ || left_) fail(Failure::SCOPE_INVALID);
    if (active == this) active = nullptr;
    left_ = true;
}
void Recorder::record(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept {
    // Retain a bounded lower bound even for unexpectedly long failure paths.
    if (attempted_ < limit_ + 1) ++attempted_; else attempted_capped_ = true;
    if (!entered_ || left_ || active != this) { fail(Failure::SCOPE_INVALID); return; }
    if (input.data() != base_ || input.size() != size_) { fail(Failure::SPAN_MISMATCH); return; }
    if (offset >= size_) { fail(Failure::OFFSET_OUT_OF_RANGE); return; }
    if (!((site >= 1 && site <= 31) || site == 1001)) { fail(Failure::SITE_INVALID); return; }
    if (events_.size() >= limit_) { fail(Failure::EVENT_LIMIT); return; }
    // reserve() has already allocated the complete bounded capacity; Event is
    // trivial and the callback performs no allocation or ownership change.
    events_.push_back({offset, site});
}
bool Recorder::complete() const noexcept {
    return entered_ && left_ && failure_ == Failure::NONE && !attempted_capped_ && attempted_ == events_.size();
}
bool Recorder::emit(std::ostream &out) noexcept {
    if (!complete()) { if (failure_ == Failure::NONE) fail(Failure::SCOPE_INVALID); return false; }
    try {
        out << "MGMEM_DEX_READER_TRACE_V1 " << events_.size() << ' ' << input_sha_ << '\n';
        for (const auto &event : events_) out << "E " << event.site << ' ' << event.offset << '\n';
        out.flush();
        if (out) return true;
    } catch (...) { /* A partial stdout cannot be treated as a complete trace. */ }
    fail(Failure::OUTPUT_FAILURE); return false;
}
bool Recorder::report_failure(std::ostream &out) const noexcept {
    try {
        out << "MGMEM_DEX_TRACE_INCOMPLETE {\"reason\":\"" << failure_name()
            << "\",\"attempted_lower_bound\":" << attempted_
            << ",\"attempted_capped\":" << (attempted_capped_ ? "true" : "false")
            << ",\"recorded\":" << events_.size() << ",\"event_limit\":" << limit_ << "}\n";
        out.flush(); return static_cast<bool>(out);
    } catch (...) { return false; }
}
void observed(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept {
    if (active) active->record(input, offset, site);
}
std::string core_module_path_hex() {
    std::string path;
#if defined(_WIN32)
    HMODULE module = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCWSTR>(reinterpret_cast<uintptr_t>(&mgmem_abi_version_v1)), &module))
        throw std::runtime_error("core_module_identity_unavailable");
    wchar_t buffer[32768];
    DWORD count = GetModuleFileNameW(module, buffer, static_cast<DWORD>(sizeof(buffer) / sizeof(buffer[0])));
    if (!count || count >= sizeof(buffer) / sizeof(buffer[0])) throw std::runtime_error("core_module_identity_unavailable");
    int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, buffer, static_cast<int>(count), nullptr, 0, nullptr, nullptr);
    if (size <= 0) throw std::runtime_error("core_module_identity_unavailable");
    path.resize(static_cast<size_t>(size));
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, buffer, static_cast<int>(count), path.data(), size, nullptr, nullptr) != size)
        throw std::runtime_error("core_module_identity_unavailable");
#else
    Dl_info info{};
    if (!dladdr(reinterpret_cast<const void *>(&mgmem_abi_version_v1), &info) || !info.dli_fname)
        throw std::runtime_error("core_module_identity_unavailable");
    path = info.dli_fname;
#endif
    if (path.empty() || path.size() > 131072) throw std::runtime_error("core_module_identity_unavailable");
    static constexpr char hex[] = "0123456789abcdef";
    std::string result; result.reserve(path.size() * 2);
    for (unsigned char byte : path) { result.push_back(hex[byte >> 4]); result.push_back(hex[byte & 15]); }
    return result;
}
}
