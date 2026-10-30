#include <mgmem/mgmem.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fstream>
#include <future>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#if defined(_WIN32)
#include <malloc.h>
#endif

namespace {
using u64 = uint64_t;
void require(bool condition, const char *message) { if (!condition) throw std::runtime_error(message); }
void check(mgmem_status status) {
    if (status != MGMEM_OK) throw std::runtime_error("Memory status " + std::to_string(status));
}
u64 multiply(u64 left, u64 right) {
    require(right == 0 || left <= UINT64_MAX / right, "size overflow");
    return left * right;
}
u64 number(const std::string &text) {
    require(!text.empty(), "empty number");
    u64 value = 0;
    for (char digit : text) {
        require(digit >= '0' && digit <= '9', "invalid number");
        const auto n = static_cast<unsigned>(digit - '0');
        require(value <= (UINT64_MAX - n) / 10, "number overflow");
        value = value * 10 + n;
    }
    return value;
}
std::string token(std::istream &input) {
    std::string result;
    char c = 0;
    while (input.get(c)) {
        if (c == '#') { std::string comment; std::getline(input, comment); continue; }
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') { result += c; break; }
    }
    while (input.get(c)) {
        if (c == ' ' || c == '\t' || c == '\r' || c == '\n') {
            if (c == '\r' && input.peek() == '\n') input.get();
            break;
        }
        require(result.size() < 32, "oversized PPM token");
        result += c;
    }
    require(!result.empty(), "truncated PPM header");
    return result;
}
std::string quoted(const std::string &text) {
    std::string result = "\"";
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') { result += '\\'; result += static_cast<char>(c); }
        else if (c >= 32 && c < 127) result += static_cast<char>(c);
        else result += '?';
    }
    return result + '"';
}

// Each provider holds the same three allocations for the entire fixed-size batch.
// Direct System deliberately makes no Memory calls, including checked arithmetic.
// API leases cover each entire allocation, never individual row slots.
struct Workspace {
    std::string provider;
    std::array<u64, 3> sizes{};
    std::array<void *, 3> pointers{};
    std::array<mgmem_handle_v1, 3> owners{};
    std::array<mgmem_borrow_v1, 3> leases{};
    mgmem_domain *domain = nullptr;
    mgmem_snapshot_v1 allocated{}, before_cleanup{}, finished{};
    u64 direct_backing = 0, direct_allocations = 0;
    bool closing = false, closed = false, cleanup_ok = true;
    bool close_while_leased = false, whole_allocation_exclusive = false;
    ~Workspace() { if (!closed) { try { finish(); } catch (...) {} } }

    void open(const std::string &kind, const std::array<u64, 3> &bytes) {
        provider = kind;
        sizes = bytes;
        if (provider != "direct") {
            const uint32_t mode = provider == "system" ? MGMEM_SYSTEM : MGMEM_ARENA;
            mgmem_domain_options_v1 options{sizeof(options), 1, 0, 0, mode, 0,
                                           64ull * 1024 * 1024, 65536, nullptr};
            check(mgmem_domain_create_v1(&options, &domain));
        }
        for (size_t i = 0; i < sizes.size(); ++i) {
            if (provider == "direct") {
#if defined(_WIN32)
                pointers[i] = _aligned_malloc(static_cast<size_t>(sizes[i]), 64);
#else
                void *address = nullptr;
                if (posix_memalign(&address, 64, static_cast<size_t>(sizes[i])) != 0) address = nullptr;
                pointers[i] = address;
#endif
                require(pointers[i] != nullptr, "direct System allocation failed");
                direct_backing += sizes[i];
                ++direct_allocations;
            } else check(mgmem_allocate_v1(domain, sizes[i], 64, &owners[i]));
        }
        if (domain) check(mgmem_domain_snapshot_v1(domain, &allocated, sizeof(allocated)));
    }
    void acquire() {
        if (!domain) return;
        // A one-byte write excludes another view of the entire allocation,
        // including its disjoint last byte when the temporary buffer is wider.
        check(mgmem_borrow_acquire_v1(domain, owners[1], 0, 1, MGMEM_WRITE, &leases[1]));
        mgmem_borrow_v1 incompatible{};
        whole_allocation_exclusive = mgmem_borrow_acquire_v1(
            domain, owners[1], sizes[1] - 1, 1, MGMEM_READ, &incompatible) == MGMEM_BUSY;
        if (incompatible.lease.id) check(mgmem_borrow_release_v1(domain, incompatible.lease));
        require(whole_allocation_exclusive, "write lease did not exclude another view");
        check(mgmem_borrow_release_v1(domain, leases[1].lease));
        leases[1] = {};
        for (size_t i = 0; i < sizes.size(); ++i) {
            check(mgmem_borrow_acquire_v1(domain, owners[i], 0, sizes[i], MGMEM_WRITE, &leases[i]));
            pointers[i] = leases[i].data;
        }
    }
    void release_leases() {
        if (!domain) return;
        for (auto &lease : leases) if (lease.lease.id) {
            check(mgmem_borrow_release_v1(domain, lease.lease));
            lease = {};
        }
        pointers = {};
    }
    void release_owners() {
        for (auto &owner : owners) if (owner.id) {
            check(mgmem_release_v1(domain, owner));
            owner = {};
        }
    }
    void close_before_worker_finishes() {
        if (!domain) return; // Direct owner remains alive until join, then is freed.
        release_owners();
        check(mgmem_domain_begin_close_v1(domain));
        closing = true;
        const bool collect_busy = mgmem_domain_collect_v1(domain) == MGMEM_BUSY;
        const bool drain_timeout = mgmem_domain_drain_v1(domain, 0) == MGMEM_TIMEOUT;
        mgmem_snapshot_v1 active{};
        check(mgmem_domain_snapshot_v1(domain, &active, sizeof(active)));
        close_while_leased = collect_busy && drain_timeout && active.active_leases == 3 &&
                             active.retiring_bytes == sizes[0] + sizes[1] + sizes[2];
        require(close_while_leased, "close reclaimed active worker storage");
    }
    void finish() {
        if (closed) return;
        if (!domain) {
            for (auto &pointer : pointers) if (pointer) {
#if defined(_WIN32)
                _aligned_free(pointer);
#else
                std::free(pointer);
#endif
                pointer = nullptr;
            }
            direct_backing = 0;
        } else {
            release_leases();
            check(mgmem_domain_snapshot_v1(domain, &before_cleanup, sizeof(before_cleanup)));
            release_owners();
            if (!closing) { check(mgmem_domain_begin_close_v1(domain)); closing = true; }
            check(mgmem_domain_collect_v1(domain));
            check(mgmem_domain_drain_v1(domain, 0));
            check(mgmem_domain_snapshot_v1(domain, &finished, sizeof(finished)));
            require(finished.state == MGMEM_CLOSED && finished.backing_bytes == 0 &&
                    finished.reserved_bytes == 0 && finished.active_allocations == 0 &&
                    finished.active_leases == 0, "workspace did not close to zero");
            check(mgmem_domain_destroy_v1(domain));
            domain = nullptr;
        }
        closed = true;
    }
};

void gray_row(const uint8_t *rgb, uint8_t *luma, u64 row, u64 width, u64 &converted) {
    for (u64 x = 0; x < width; ++x) {
        const u64 input = 3 * (row * width + x);
        luma[x] = static_cast<uint8_t>((77u * rgb[input] + 150u * rgb[input+1] +
                                      29u * rgb[input+2]) >> 8);
        ++converted;
    }
}
u64 process(const uint8_t *rgb, uint8_t *temporary, uint8_t *output,
            u64 width, u64 height, bool ring, u64 &converted) {
    const u64 rows = std::min<u64>(height, 3);
    if (!ring) for (u64 y = 0; y < height; ++y)
        gray_row(rgb, temporary + y * width, y, width, converted);
    u64 next_row = 0, checksum = 0;
    for (u64 y = 0; y < height; ++y) {
        // Load the next needed row before overwriting the slot whose last read
        // was in the preceding output row. Edge rows alias an existing slot.
        if (ring) while (next_row <= std::min(height - 1, y + 1)) {
            gray_row(rgb, temporary + (next_row % rows) * width, next_row, width, converted);
            ++next_row;
        }
        for (u64 x = 0; x < width; ++x) {
            unsigned sum = 0;
            for (int dy = -1; dy <= 1; ++dy) {
                const u64 yy = dy < 0 ? (y ? y-1 : 0) : (dy > 0 ? std::min(y+1, height-1) : y);
                const auto *row = temporary + (ring ? yy % rows : yy) * width;
                for (int dx = -1; dx <= 1; ++dx) {
                    const u64 xx = dx < 0 ? (x ? x-1 : 0) : (dx > 0 ? std::min(x+1, width-1) : x);
                    sum += row[xx];
                }
            }
            output[y * width + x] = static_cast<uint8_t>(sum / 9);
            checksum += output[y * width + x];
        }
    }
    return checksum;
}
} // namespace

int main(int argc, char **argv) {
    Workspace workspace;
    std::string provider = "direct", layout = "full", failure;
    bool close_race = false, worker_fault = false;
    u64 frames = 1, pixels = 0, width = 0, height = 0, converted = 0;
    u64 completed = 0, checksum = 0, payload = 0, source_capacity = 0;
    u64 peak_backing = 0, allocations = 0, provider_allocations = 0, elapsed_ns = 0;
    bool started = false;
    std::chrono::steady_clock::time_point start{};
    try {
        require(argc >= 3, "usage: mgmem_image_paired input.ppm output.pgm --provider direct|system|arena --layout full|ring [--frames 1..32] [--close-race] [--fault-worker]");
        for (int i = 3; i < argc; ++i) {
            const std::string option = argv[i];
            if (option == "--close-race") close_race = true;
            else if (option == "--fault-worker") worker_fault = true;
            else {
                require(i + 1 < argc, "missing option value");
                const std::string value = argv[++i];
                if (option == "--provider") provider = value;
                else if (option == "--layout") layout = value;
                else if (option == "--frames") frames = number(value);
                else throw std::runtime_error("unknown option");
            }
        }
        require(provider == "direct" || provider == "system" || provider == "arena", "invalid provider");
        require(layout == "full" || layout == "ring", "invalid layout");
        require(frames >= 1 && frames <= 32, "frames outside bounded batch");
        std::ifstream input(argv[1], std::ios::binary);
        require(input.is_open() && token(input) == "P6", "expected P6 PPM input");
        width = number(token(input)); height = number(token(input));
        require(width > 0 && height > 0 && number(token(input)) == 255, "expected nonempty 8-bit RGB input");
        pixels = multiply(width, height);
        const u64 rgb_bytes = multiply(pixels, 3);
        require(pixels <= 8 * 1024 * 1024 && rgb_bytes <= SIZE_MAX, "image exceeds bounded workspace");
        std::vector<uint8_t> source(static_cast<size_t>(rgb_bytes));
        input.read(reinterpret_cast<char *>(source.data()), static_cast<std::streamsize>(rgb_bytes));
        require(static_cast<u64>(input.gcount()) == rgb_bytes && input.peek() == EOF, "truncated input or trailing bytes");
        source_capacity = source.capacity();
        const u64 temporary_bytes = layout == "ring" ? multiply(std::min<u64>(height, 3), width) : pixels;
        const std::array<u64, 3> sizes{rgb_bytes, temporary_bytes, pixels};
        payload = rgb_bytes + temporary_bytes + pixels;
        start = std::chrono::steady_clock::now(); started = true;
        workspace.open(provider, sizes);
        peak_backing = provider == "direct" ? workspace.direct_backing : workspace.allocated.peak_backing_bytes;
        allocations = provider == "direct" ? workspace.direct_allocations : workspace.allocated.allocation_count;
        provider_allocations = provider == "direct" ? workspace.direct_allocations : workspace.allocated.provider_allocation_count;
        for (u64 frame = 0; frame < frames; ++frame) {
            workspace.acquire();
            std::promise<void> accepted;
            auto admission = accepted.get_future();
            std::packaged_task<u64()> operation([&] {
                try {
                    admission.get();
                    std::memcpy(workspace.pointers[0], source.data(), static_cast<size_t>(rgb_bytes));
                    if (worker_fault) throw std::runtime_error("injected worker exception");
                    u64 count = 0;
                    const u64 result = process(static_cast<const uint8_t *>(workspace.pointers[0]),
                        static_cast<uint8_t *>(workspace.pointers[1]), static_cast<uint8_t *>(workspace.pointers[2]),
                        width, height, layout == "ring", count);
                    require(count == pixels, "gray pixel conversion count differs");
                    converted += count;
                    if (frame + 1 == frames) {
                        std::ofstream output(argv[2], std::ios::binary);
                        require(output.is_open(), "cannot open output file");
                        output << "P5\n" << width << ' ' << height << "\n255\n";
                        output.write(static_cast<const char *>(workspace.pointers[2]), static_cast<std::streamsize>(pixels));
                        output.close();
                        require(output.good(), "failed output write");
                    }
                    workspace.release_leases();
                    return result;
                } catch (...) {
                    workspace.release_leases();
                    throw;
                }
            });
            auto result = operation.get_future();
            std::thread worker(std::move(operation));
            try {
                // The worker exists and owns its admitted leases, but waits
                // before its first data access. Close cannot revoke its storage.
                if (close_race && frame + 1 == frames) workspace.close_before_worker_finishes();
                accepted.set_value();
            } catch (...) {
                const auto admission_error = std::current_exception();
                try { accepted.set_exception(admission_error); } catch (...) {}
                worker.join();
                std::rethrow_exception(admission_error);
            }
            worker.join();
            checksum += result.get();
            ++completed;
        }
        workspace.finish();
        // Include the final release of the common preloaded source in all modes.
        std::vector<uint8_t>().swap(source);
        elapsed_ns = static_cast<u64>(std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - start).count());
    } catch (const std::exception &error) {
        failure = error.what();
        try { workspace.finish(); }
        catch (const std::exception &cleanup_error) { workspace.cleanup_ok = false; failure += "; cleanup: "; failure += cleanup_error.what(); }
        if (started) elapsed_ns = static_cast<u64>(std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - start).count());
    }
    const bool success = failure.empty();
    const u64 final_provider_allocations = provider == "direct" ? workspace.direct_allocations :
                                          workspace.before_cleanup.provider_allocation_count;
    const bool provider_count_valid = workspace.closed && final_provider_allocations >= provider_allocations;
    std::cout << "{\"schema_version\":1,\"consumer\":\"image_workspace_v2\",\"scope\":\"HOST_FUNCTIONAL_RESOURCE_DIAGNOSTIC\",\"success\":"
              << (success ? "true" : "false") << ",\"provider\":" << quoted(provider) << ",\"layout\":" << quoted(layout)
              << ",\"width\":" << width << ",\"height\":" << height << ",\"pixels\":" << pixels
              << ",\"frames\":" << frames << ",\"completed_frames\":" << completed << ",\"checksum\":" << checksum
              << ",\"gray_conversions\":" << converted << ",\"logical_payload_bytes\":" << payload
              << ",\"peak_unique_owned_backing_bytes\":" << peak_backing << ",\"external_source_capacity_bytes\":" << source_capacity
              << ",\"allocation_count\":" << allocations << ",\"provider_allocation_count\":" << final_provider_allocations
              << ",\"initial_provider_allocation_count\":" << provider_allocations
              << ",\"steady_state_provider_allocations\":";
    if (provider_count_valid) std::cout << final_provider_allocations - provider_allocations;
    else std::cout << "null";
    std::cout << ",\"arena_chunk_bytes\":" << (provider == "arena" ? 65536 : 0)
              << ",\"retained_bytes_while_owned\":" << workspace.allocated.retained_bytes
              << ",\"reserved_bytes_while_owned\":" << workspace.allocated.reserved_bytes
              << ",\"metadata_bytes\":null,\"rss_pss_bytes\":null,\"arena_used_bytes\":null"
              << ",\"closed_backing_bytes\":" << (workspace.domain ? workspace.finished.backing_bytes : workspace.direct_backing)
              << ",\"closed\":" << (workspace.closed ? "true" : "false") << ",\"cleanup_ok\":" << (workspace.cleanup_ok ? "true" : "false")
              << ",\"whole_allocation_exclusive\":" << (provider == "direct" ? "null" : workspace.whole_allocation_exclusive ? "true" : "false")
              << ",\"close_race_requested\":" << (close_race ? "true" : "false")
              << ",\"close_while_leased_verified\":" << (provider == "direct" ? "null" : workspace.close_while_leased ? "true" : "false")
              << ",\"diagnostic_total_ns\":" << elapsed_ns << ",\"performance_qualification\":\"NOT_STARTED\",\"error\":" << quoted(failure) << "}\n";
    return success ? 0 : 1;
}
