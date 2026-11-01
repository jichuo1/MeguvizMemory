#include "reader.hpp"
#include "trace_runtime.hpp"
#include "trace_sha256.hpp"
#include <cstdint>
#include <fstream>
#include <iostream>
#include <memory_resource>
#include <span>
#include <string>
#include <vector>

namespace {
struct Event { uint64_t offset; uint32_t site; };
struct Recorder {
    const uint8_t *base = nullptr;
    size_t size = 0;
    size_t limit = 0;
    uint64_t attempted = 0;
    bool incomplete = false;
    std::vector<Event> events;
};
thread_local Recorder *active = nullptr;
void number(std::string &s, uint64_t n) { s += std::to_string(n); s += ':'; }
void text(std::string &s, std::u16string_view value) {
    number(s, value.size());
    for (char16_t c : value) number(s, c);
}
std::string fingerprint(const dexmeguviz::Facts &f) {
    std::string s;
    number(s, f.dex_version); number(s, f.string_count); number(s, f.type_count);
    number(s, f.class_count); number(s, f.method_ref_count); number(s, f.call_site_count);
    number(s, f.method_handle_count);
    number(s, f.strings.size()); for (const auto &v : f.strings) text(s, v);
    number(s, f.types.size()); for (auto v : f.types) number(s, v);
    number(s, f.prototypes.size()); for (const auto &v : f.prototypes) {
        number(s, v.return_type); number(s, v.parameters.size());
        for (auto p : v.parameters) number(s, p); text(s, v.descriptor);
    }
    number(s, f.method_refs.size()); for (const auto &v : f.method_refs) {
        number(s, v.owner); number(s, v.type); number(s, v.name);
    }
    number(s, f.field_refs.size()); for (const auto &v : f.field_refs) {
        number(s, v.owner); number(s, v.type); number(s, v.name);
    }
    number(s, f.methods.size()); for (const auto &v : f.methods) {
        number(s, v.method_index); number(s, v.access_flags); number(s, v.declaration_offset);
        number(s, v.code_offset); number(s, v.code_end); text(s, v.owner); text(s, v.name); text(s, v.prototype);
    }
    number(s, f.class_definitions.size()); for (const auto &v : f.class_definitions) {
        number(s, v.type_id); number(s, v.definition_offset); number(s, v.access_flags);
        number(s, v.superclass_type); number(s, v.class_data_offset); number(s, v.annotations_offset);
        number(s, v.static_values_offset); number(s, v.interfaces.size()); for (auto x : v.interfaces) number(s, x);
    }
    number(s, f.field_definitions.size()); for (const auto &v : f.field_definitions) {
        number(s, v.field_ref_index); number(s, v.access_flags); number(s, v.declaration_offset); number(s, v.owner_type);
    }
    for (const auto *directory : {&f.method_definition_by_ref, &f.field_definition_by_ref, &f.class_definition_by_type}) {
        number(s, directory->size()); for (auto v : *directory) number(s, v);
    }
    return image_trace::sha256_hex(reinterpret_cast<const unsigned char *>(s.data()), s.size());
}
}

namespace mgmem_dex_trace {
void observed(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept {
    if (!active) return;
    auto &r = *active;
    ++r.attempted;
    if (input.data() != r.base || input.size() != r.size || offset >= r.size ||
        !((site >= 1 && site <= 31) || site == 1001) || r.events.size() >= r.limit) {
        r.incomplete = true;
        return;
    }
    r.events.push_back({offset, site});
}
}

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    try {
        size_t used = 0;
        const auto max_events = std::stoull(argv[2], &used);
        if (argv[2][used] != '\0' || max_events == 0 || max_events > 1000000) return 2;
        std::ifstream input(argv[1], std::ios::binary | std::ios::ate);
        if (!input) return 2;
        const auto end = input.tellg();
        if (end <= 0 || end > 32 * 1024 * 1024) return 2;
        std::vector<uint8_t> bytes(static_cast<size_t>(end));
        input.seekg(0); if (!input.read(reinterpret_cast<char *>(bytes.data()), end)) return 2;
        const auto input_sha = image_trace::sha256_hex(bytes.data(), bytes.size());
        Recorder recorder; recorder.base = bytes.data(); recorder.size = bytes.size(); recorder.limit = static_cast<size_t>(max_events);
        recorder.events.reserve(static_cast<size_t>(max_events));
#if defined(MGMEM_DEX_METADATA_TRACE)
        active = &recorder;
#endif
        std::string result = "OK", digest = "-";
        try { auto facts = dexmeguviz::read_metadata(bytes); digest = fingerprint(facts); }
        catch (const dexmeguviz::ReadError &error) {
            result = "ERROR_" + std::to_string(static_cast<int>(error.code)) + "_" + std::to_string(error.offset);
        }
        active = nullptr;
#if defined(MGMEM_DEX_METADATA_TRACE)
        const char *capture = recorder.incomplete ? "INCOMPLETE" : "COMPLETE";
#else
        const char *capture = "NOT_INSTRUMENTED";
#endif
        std::cout << "R " << result << ' ' << digest << ' ' << input_sha << ' ' << bytes.size() << ' '
                  << recorder.attempted << ' ' << recorder.events.size() << ' ' << capture << '\n';
        for (const auto &event : recorder.events) std::cout << "E " << event.site << ' ' << event.offset << '\n';
        std::cout.flush();
        return !std::cout || recorder.incomplete ? 3 : 0;
    } catch (...) { active = nullptr; return 2; }
}
