#ifndef MGMEM_IMAGE_TRACE_CONTRACT_HPP
#define MGMEM_IMAGE_TRACE_CONTRACT_HPP

#include <array>
#include <cstdio>
#include <cstdint>
#include <filesystem>
#include <limits>
#include <stdexcept>
#include <string>

// Diagnostic-only, compiled solely into the opt-in trace executable. No data
// bytes, file names, raw addresses, timestamps, or physical-page claims enter
// the trace. The controlled main/worker barrier serializes all record calls.
namespace image_trace {
using u64 = std::uint64_t;
constexpr u64 maximum_events = 64000000;
constexpr u64 maximum_file_bytes = 2ull * 1024 * 1024 * 1024;
constexpr unsigned no_allocation = 255;

inline std::FILE *open_exclusive(const std::filesystem::path &path) noexcept {
    std::FILE *output = nullptr;
#if defined(_MSC_VER)
    if (_wfopen_s(&output, path.c_str(), L"wbx") != 0) output = nullptr;
#elif defined(_WIN32)
    output = _wfopen(path.c_str(), L"wbx");
#else
    output = std::fopen(path.c_str(), "wbx");
#endif
    return output;
}

inline bool write_output_exclusive(const std::filesystem::path &path, const std::string &header,
                                   const void *data, size_t length) noexcept {
    std::FILE *output = open_exclusive(path);
    if (!output) return false;
    bool success = std::fwrite(header.data(), 1, header.size(), output) == header.size() &&
                   std::fwrite(data, 1, length, output) == length;
    if (std::fflush(output) != 0) success = false;
    if (std::fclose(output) != 0) success = false;
    return success;
}

class Capture {
    std::array<char, 65536> buffer_{};
    std::FILE *output_ = nullptr;
    std::array<std::uintptr_t, 3> bases_{};
    std::array<u64, 3> sizes_{};
    u64 limit_, attempted_ = 0, written_ = 0, bytes_ = 0;
    bool incomplete_ = false;
#if defined(MGMEM_IMAGE_TRACE_TESTING)
    u64 fail_at_ = 0;
#endif
public:
    Capture(const std::filesystem::path &directory, u64 limit, u64 width, u64 height, u64 frames,
            const std::string &provider, const std::string &layout, bool close_race,
            const std::string &rgb_sha256) : limit_(limit) {
        if (limit == 0 || limit > maximum_events) throw std::runtime_error("invalid_trace_limit");
        if (rgb_sha256.size() != 64) throw std::runtime_error("invalid_trace_payload_hash");
        // Prepare all potentially allocating values before owning FILE*;
        // failed construction cannot rely on the object's destructor.
        const auto target = directory / "events.txt";
        const std::string header = "MGMEM_IMAGE_TRACE_V1\nH " + std::to_string(width) + ' ' +
            std::to_string(height) + ' ' + std::to_string(frames) + ' ' + provider + ' ' + layout + ' ' +
            std::to_string(close_race ? 1 : 0) + ' ' + std::to_string(limit) + "\nI " + rgb_sha256 + '\n';
        std::error_code error;
        if (!std::filesystem::create_directory(directory, error) || error)
            throw std::runtime_error("trace_directory_not_fresh_or_unavailable");
        output_ = open_exclusive(target);
        if (!output_) throw std::runtime_error("trace_open_failed");
        if (std::setvbuf(output_, buffer_.data(), _IOFBF, buffer_.size()) != 0) {
            std::fclose(output_); output_ = nullptr; throw std::runtime_error("trace_buffer_failed");
        }
        const auto header_written = std::fwrite(header.data(), 1, header.size(), output_);
        bytes_ = static_cast<u64>(header.size());
        if (header_written != header.size()) {
            std::fclose(output_); output_ = nullptr; throw std::runtime_error("trace_header_write_failed");
        }
    }
    ~Capture() { if (output_) std::fclose(output_); }
    Capture(const Capture &) = delete;
    Capture &operator=(const Capture &) = delete;
#if defined(MGMEM_IMAGE_TRACE_TESTING)
    // Only a test executable may force an output failure; production trace
    // builds have neither this method nor a corresponding command option.
    void test_fail_at(u64 attempt) noexcept { fail_at_ = attempt; }
#endif
    void bounds(const std::array<void *, 3> &pointers, const std::array<u64, 3> &sizes) noexcept {
        for (size_t i = 0; i < 3; ++i) {
            bases_[i] = reinterpret_cast<std::uintptr_t>(pointers[i]); sizes_[i] = sizes[i];
            if (!bases_[i] || sizes[i] > (std::numeric_limits<std::uintptr_t>::max)() - bases_[i]) incomplete_ = true;
        }
    }
    void record(char actor, const char *operation, unsigned allocation, u64 offset, u64 length) noexcept {
        if (attempted_ != (std::numeric_limits<u64>::max)()) ++attempted_;
        else { incomplete_ = true; return; }
#if defined(MGMEM_IMAGE_TRACE_TESTING)
        if (fail_at_ && attempted_ == fail_at_) {
            if (output_) { std::fclose(output_); output_ = nullptr; }
            incomplete_ = true;
            return;
        }
#endif
        if (incomplete_ || written_ >= limit_) { incomplete_ = true; return; }
        char line[160]{};
        const int length_written = std::snprintf(line, sizeof(line), "E %llu %c %s %u %llu %llu\n",
            static_cast<unsigned long long>(attempted_), actor, operation, allocation,
            static_cast<unsigned long long>(offset), static_cast<unsigned long long>(length));
        if (length_written <= 0 || static_cast<size_t>(length_written) >= sizeof(line) ||
            bytes_ > maximum_file_bytes - 256 || static_cast<u64>(length_written) > maximum_file_bytes - 256 - bytes_) {
            incomplete_ = true; return;
        }
        if (std::fwrite(line, 1, static_cast<size_t>(length_written), output_) != static_cast<size_t>(length_written)) {
            incomplete_ = true; return;
        }
        bytes_ += static_cast<u64>(length_written); ++written_;
    }
    void access(const char *operation, unsigned allocation, const void *address) noexcept {
        const auto pointer = reinterpret_cast<std::uintptr_t>(address);
        if (allocation >= 3 || !bases_[allocation] || pointer < bases_[allocation] ||
            pointer - bases_[allocation] >= sizes_[allocation]) { incomplete_ = true; return; }
        record('W', operation, allocation, static_cast<u64>(pointer - bases_[allocation]), 1);
    }
    bool complete() const noexcept { return !incomplete_; }
    bool finish(bool success, u64 checksum, u64 converted, u64 completed) noexcept {
        try {
            // Observe buffered event-write errors before choosing the footer.
            // A later footer/close failure is still an unknown on-disk outcome;
            // callers must bind the file to the capture process result.
            const bool events_flushed = output_ && std::fflush(output_) == 0;
            const bool valid = success && !incomplete_ && attempted_ == written_ && events_flushed;
            char footer[192]{};
            const int size = std::snprintf(footer, sizeof(footer), "END %s %llu %llu %llu %llu %llu\n",
                valid ? "COMPLETE" : "INCOMPLETE", static_cast<unsigned long long>(written_),
                static_cast<unsigned long long>(attempted_), static_cast<unsigned long long>(checksum),
                static_cast<unsigned long long>(converted), static_cast<unsigned long long>(completed));
            bool good = size > 0 && static_cast<size_t>(size) < sizeof(footer) && output_ &&
                std::fwrite(footer, 1, static_cast<size_t>(size), output_) == static_cast<size_t>(size);
            if (output_) {
                if (std::fflush(output_) != 0) good = false;
                if (std::fclose(output_) != 0) good = false;
                output_ = nullptr;
            }
            return good && valid;
        } catch (...) { return false; }
    }
};
} // namespace image_trace
#endif
