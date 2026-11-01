#include "stream_trace.hpp"
#include "trace_runtime.hpp"
#include "trace_sha256.hpp"
#include <mgmem/mgmem.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <ostream>
#include <stdexcept>
#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <fcntl.h>
#include <io.h>
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace {
thread_local mgmem_dex_trace_v2::Recorder *active = nullptr;

uint64_t event_limit() {
    const char *text = std::getenv("MGMEM_DEX_TRACE_MAX_EVENTS");
    if (!text) return mgmem_dex_trace_v2::DEFAULT_EVENTS;
    if (!*text) throw std::runtime_error("trace_limit_invalid");
    uint64_t value = 0;
    for (const char *p = text; *p; ++p) {
        if (*p < '0' || *p > '9' || value > (mgmem_dex_trace_v2::MAX_EVENTS - unsigned(*p - '0')) / 10u)
            throw std::runtime_error("trace_limit_invalid");
        value = value * 10u + unsigned(*p - '0');
    }
    if (!value || value > mgmem_dex_trace_v2::MAX_EVENTS) throw std::runtime_error("trace_limit_invalid");
    return value;
}
void le16(uint8_t *out, uint16_t value) noexcept {
    out[0] = static_cast<uint8_t>(value); out[1] = static_cast<uint8_t>(value >> 8);
}
void le32(uint8_t *out, uint32_t value) noexcept {
    for (unsigned i = 0; i != 4; ++i) out[i] = static_cast<uint8_t>(value >> (8 * i));
}
void le64(uint8_t *out, uint64_t value) noexcept {
    for (unsigned i = 0; i != 8; ++i) out[i] = static_cast<uint8_t>(value >> (8 * i));
}
}

namespace mgmem_dex_trace_v2 {
bool configure_stdout_binary() noexcept {
#if defined(_WIN32)
    return _setmode(_fileno(stdout), _O_BINARY) != -1;
#else
    return true;
#endif
}

Recorder::Recorder(std::ostream &output) : output_(output), limit_(event_limit()) {}
void Recorder::fail(Failure reason) noexcept { if (failure_ == Failure::NONE) failure_ = reason; }
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
bool Recorder::flush_buffer() noexcept {
    if (!used_) return true;
    try {
        output_.write(reinterpret_cast<const char *>(buffer_.data()), static_cast<std::streamsize>(used_));
        if (!output_) { fail(Failure::OUTPUT_FAILURE); return false; }
        used_ = 0;
        return true;
    } catch (...) { fail(Failure::OUTPUT_FAILURE); return false; }
}
bool Recorder::append(const uint8_t *data, std::size_t size) noexcept {
    if (size > buffer_.size()) { fail(Failure::OUTPUT_FAILURE); return false; }
    if (buffer_.size() - used_ < size && !flush_buffer()) return false;
    std::memcpy(buffer_.data() + used_, data, size);
    used_ += size;
    return true;
}
void Recorder::enter(const void *base, uint64_t size) {
    if (active || entered_ || !base || !size || size > MAX_INPUT || size > std::numeric_limits<uint32_t>::max()) {
        fail(Failure::SCOPE_INVALID); throw std::runtime_error("trace_scope_invalid");
    }
    try {
        base_ = static_cast<const uint8_t *>(base); size_ = size;
        const auto digest = image_trace::sha256_hex(base_, static_cast<std::size_t>(size_));
        if (digest.size() != 64) { fail(Failure::SCOPE_INVALID); throw std::runtime_error("trace_scope_invalid"); }
        uint8_t header[HEADER_BYTES]{};
        std::memcpy(header, "MDXTRC2\n", 8);
        le64(header + 8, size_); le64(header + 16, limit_);
        std::memcpy(header + 24, digest.data(), 64);
        if (!append(header, sizeof(header))) throw std::runtime_error("trace_output_failed");
        entered_ = true; active = this;
    } catch (...) {
        if (failure_ == Failure::NONE) fail(Failure::SCOPE_INVALID);
        throw;
    }
}
void Recorder::leave() noexcept {
    if (active != this || !entered_ || left_) fail(Failure::SCOPE_INVALID);
    if (active == this) active = nullptr;
    left_ = true;
}
void Recorder::record(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept {
    if (attempted_ < limit_ + 1) ++attempted_; else attempted_capped_ = true;
    if (!entered_ || left_ || active != this) { fail(Failure::SCOPE_INVALID); return; }
    if (input.data() != base_ || input.size() != size_) { fail(Failure::SPAN_MISMATCH); return; }
    if (offset >= size_ || offset > std::numeric_limits<uint32_t>::max()) { fail(Failure::OFFSET_OUT_OF_RANGE); return; }
    if (!((site >= 1 && site <= 31) || site == 1001)) { fail(Failure::SITE_INVALID); return; }
    if (recorded_ >= limit_) { fail(Failure::EVENT_LIMIT); return; }
    // After any failure, retain the first category; do not append more bytes.
    if (failure_ != Failure::NONE) return;
    uint8_t event[7]{}; event[0] = 1;
    le16(event + 1, static_cast<uint16_t>(site)); le32(event + 3, static_cast<uint32_t>(offset));
    if (append(event, sizeof(event))) ++recorded_;
}
bool Recorder::complete() const noexcept {
    return entered_ && left_ && !finished_ && failure_ == Failure::NONE && !attempted_capped_ && attempted_ == recorded_;
}
bool Recorder::finish() noexcept {
    if (!complete()) { if (failure_ == Failure::NONE) fail(Failure::SCOPE_INVALID); return false; }
    if (!flush_buffer()) return false;
    try {
        output_.flush();
        if (!output_) { fail(Failure::OUTPUT_FAILURE); return false; }
        uint8_t trailer[9]{}; trailer[0] = 2; le64(trailer + 1, recorded_);
        output_.write(reinterpret_cast<const char *>(trailer), sizeof(trailer));
        if (!output_) { fail(Failure::OUTPUT_FAILURE); return false; }
        finished_ = true;
        return true;
    } catch (...) { fail(Failure::OUTPUT_FAILURE); return false; }
}
bool Recorder::report_failure(std::ostream &out) const noexcept {
    try {
        out << "MGMEM_DEX_TRACE_INCOMPLETE {\"reason\":\"" << failure_name()
            << "\",\"attempted_lower_bound\":" << attempted_
            << ",\"attempted_capped\":" << (attempted_capped_ ? "true" : "false")
            << ",\"recorded\":" << recorded_ << ",\"event_limit\":" << limit_ << "}\n";
        out.flush(); return static_cast<bool>(out);
    } catch (...) { return false; }
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
    path.resize(static_cast<std::size_t>(size));
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

namespace mgmem_dex_trace {
void observed(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept {
    if (active) active->record(input, offset, site);
}
}
