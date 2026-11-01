#pragma once
#include <cstddef>
#include <cstdint>
#include <iosfwd>
#include <span>
#include <string>

// Diagnostic only. Memory used for counting is independent of event count.
namespace mgmem_dex_trace {
class Recorder {
public:
    Recorder() = default;
    Recorder(const Recorder &) = delete;
    Recorder &operator=(const Recorder &) = delete;
    void enter(const void *base, uint64_t size);
    void leave() noexcept;
    void record(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept;
    bool complete() const noexcept;
    bool emit(std::ostream &output) noexcept;
    bool report_failure(std::ostream &output) const noexcept;
#if defined(MGMEM_COUNT_TESTING)
    void test_set_count(uint64_t value) noexcept { count_ = value; }
#endif
private:
    const uint8_t *base_ = nullptr;
    uint64_t size_ = 0, count_ = 0;
    uint32_t sites_ = 0;
    bool entered_ = false, left_ = false, failed_ = false;
    std::string input_sha_;
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
std::string core_module_path_hex();
}
