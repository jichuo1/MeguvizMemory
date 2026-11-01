#include "consumer_trace.hpp"
#include "trace_runtime.hpp"
#include "trace_sha256.hpp"
#include <mgmem/mgmem.h>
#include <limits>
#include <ostream>
#include <stdexcept>
#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace { thread_local mgmem_dex_trace::Recorder *active = nullptr; }
namespace mgmem_dex_trace {
void Recorder::enter(const void *base, uint64_t size) {
    if (active || entered_ || !base || !size || size > std::numeric_limits<size_t>::max()) {
        failed_ = true; throw std::runtime_error("count_scope_invalid");
    }
    try {
        base_ = static_cast<const uint8_t *>(base); size_ = size;
        input_sha_ = image_trace::sha256_hex(base_, static_cast<size_t>(size_));
        entered_ = true; active = this;
    } catch (...) { failed_ = true; throw; }
}
void Recorder::leave() noexcept {
    if (active != this || !entered_ || left_) failed_ = true;
    if (active == this) active = nullptr;
    left_ = true;
}
void Recorder::record(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept {
    if (failed_) return;
    if (!entered_ || left_ || active != this || input.data() != base_ || input.size() != size_ ||
        offset >= size_ || !((site >= 1 && site <= 31) || site == 1001) ||
        count_ == std::numeric_limits<uint64_t>::max()) { failed_ = true; return; }
    ++count_;
    sites_ |= uint32_t{1} << (site == 1001 ? 31 : site - 1);
}
bool Recorder::complete() const noexcept { return entered_ && left_ && !failed_; }
bool Recorder::emit(std::ostream &out) noexcept {
    if (!complete()) return false;
    try {
        out << "MGMEM_DEX_READER_COUNT_V1 " << count_ << ' ' << sites_ << ' ' << input_sha_ << '\n';
        out.flush();
        if (out) return true;
    } catch (...) {}
    failed_ = true; return false;
}
bool Recorder::report_failure(std::ostream &out) const noexcept {
    try { out << "MGMEM_DEX_READER_COUNT_INCOMPLETE\n"; out.flush(); return static_cast<bool>(out); }
    catch (...) { return false; }
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
