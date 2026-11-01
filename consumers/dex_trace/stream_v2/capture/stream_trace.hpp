#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <iosfwd>
#include <span>
#include <string>

// Independent V2 diagnostic: one snapshot-open call, one calling thread.
// It is not a Memory ABI or an instrumentation of the ordinary consumer.
namespace mgmem_dex_trace_v2 {
constexpr uint64_t MAX_EVENTS = 64000000;
constexpr uint64_t DEFAULT_EVENTS = 250000;
constexpr uint64_t MAX_INPUT = 32ull * 1024 * 1024;
constexpr std::size_t HEADER_BYTES = 88;

bool configure_stdout_binary() noexcept;
std::string core_module_path_hex();

class Recorder {
public:
    explicit Recorder(std::ostream &output);
    Recorder(const Recorder &) = delete;
    Recorder &operator=(const Recorder &) = delete;
    void enter(const void *base, uint64_t size);
    void leave() noexcept;
    void record(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept;
    bool complete() const noexcept;
    // A footer alone never proves success: the producer must also emit its
    // final report, flush successfully and exit zero.
    bool finish() noexcept;
    bool report_failure(std::ostream &output) const noexcept;
private:
    enum class Failure { NONE, EVENT_LIMIT, SPAN_MISMATCH, OFFSET_OUT_OF_RANGE,
                         SITE_INVALID, SCOPE_INVALID, OUTPUT_FAILURE };
    void fail(Failure reason) noexcept;
    const char *failure_name() const noexcept;
    bool append(const uint8_t *data, std::size_t size) noexcept;
    bool flush_buffer() noexcept;
    std::ostream &output_;
    const uint8_t *base_ = nullptr;
    uint64_t size_ = 0, limit_ = 0, attempted_ = 0, recorded_ = 0;
    bool entered_ = false, left_ = false, finished_ = false, attempted_capped_ = false;
    Failure failure_ = Failure::NONE;
    std::array<uint8_t, 65536> buffer_{};
    std::size_t used_ = 0;
};

class Scope {
public:
    Scope(Recorder &recorder, const void *base, uint64_t size) : recorder_(recorder) { recorder_.enter(base, size); }
    ~Scope() { recorder_.leave(); }
    Scope(const Scope &) = delete;
    Scope &operator=(const Scope &) = delete;
private:
    Recorder &recorder_;
};
}
