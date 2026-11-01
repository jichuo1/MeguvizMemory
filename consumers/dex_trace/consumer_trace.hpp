#pragma once
#include <cstddef>
#include <cstdint>
#include <iosfwd>
#include <string>
#include <span>
#include <vector>

// Diagnostic-only, one snapshot-open call on the calling thread. The public
// Dex and Memory APIs and the normal consumer target do not include this type.
namespace mgmem_dex_trace {
struct Event { uint64_t offset; uint32_t site; };
class Recorder {
public:
    Recorder();
    Recorder(const Recorder &) = delete;
    Recorder &operator=(const Recorder &) = delete;
    void enter(const void *base, uint64_t size);
    void leave() noexcept;
    void record(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept;
    bool complete() const noexcept;
    bool emit(std::ostream &output) const;
private:
    const uint8_t *base_ = nullptr;
    uint64_t size_ = 0, attempted_ = 0;
    size_t limit_ = 0;
    bool entered_ = false, left_ = false, incomplete_ = false;
    std::string input_sha_;
    std::vector<Event> events_;
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
// Hex-encoded UTF-8 OS module path; callers compare it privately against the
// installed Memory core and hash that file. This is not mapped-byte proof.
std::string core_module_path_hex();
}
