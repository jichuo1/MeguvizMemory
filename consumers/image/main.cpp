#include <mgmem/mgmem.h>
#include <array>
#include <cstdint>
#include <fstream>
#include <future>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
void check(mgmem_status s) { if (s != MGMEM_OK) throw std::runtime_error("Memory status " + std::to_string(s)); }
void require(bool value, const char *message) { if (!value) throw std::runtime_error(message); }
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
uint64_t number(const std::string &value) {
    uint64_t n = 0;
    for (char c : value) {
        require(c >= '0' && c <= '9' && n <= (UINT64_MAX - 9) / 10, "invalid PPM number");
        n = n * 10 + static_cast<unsigned>(c - '0');
    }
    return n;
}
struct Workspace {
    mgmem_domain *domain = nullptr;
    std::array<mgmem_handle_v1, 3> allocations{};
    std::array<mgmem_borrow_v1, 3> borrows{};
    ~Workspace() {
        if (!domain) return;
        for (auto &b : borrows) if (b.lease.id) (void)mgmem_borrow_release_v1(domain, b.lease);
        for (auto &a : allocations) if (a.id) (void)mgmem_release_v1(domain, a);
        (void)mgmem_domain_begin_close_v1(domain);
        (void)mgmem_domain_collect_v1(domain);
        if (mgmem_domain_drain_v1(domain, 0) == MGMEM_OK) (void)mgmem_domain_destroy_v1(domain);
    }
};
}

int main(int argc, char **argv) {
    try {
        require(argc == 3 || (argc == 4 && std::string(argv[3]) == "--system"),
                "usage: mgmem_image input.ppm output.pgm [--system]");
        std::ifstream input(argv[1], std::ios::binary);
        require(input.is_open() && token(input) == "P6", "expected a P6 PPM input");
        const uint64_t width = number(token(input)), height = number(token(input));
        require(number(token(input)) == 255 && width > 0 && height > 0, "expected nonempty 8-bit RGB input");
        uint64_t pixels = 0, rgb_bytes = 0;
        check(mgmem_checked_mul_v1(width, height, &pixels));
        check(mgmem_checked_mul_v1(pixels, 3, &rgb_bytes));
        require(pixels <= 8 * 1024 * 1024 && rgb_bytes <= SIZE_MAX, "image exceeds bounded workspace");
        Workspace work;
        const uint32_t mode = argc == 4 ? MGMEM_SYSTEM : MGMEM_ARENA;
        mgmem_domain_options_v1 options{sizeof(options), 1, 0, 0, mode, 0, 64ull * 1024 * 1024, 65536, nullptr};
        check(mgmem_domain_create_v1(&options, &work.domain));
        const std::array<uint64_t, 3> sizes{rgb_bytes, pixels, pixels};
        for (size_t i = 0; i < sizes.size(); ++i) {
            check(mgmem_allocate_v1(work.domain, sizes[i], 64, &work.allocations[i]));
            check(mgmem_borrow_acquire_v1(work.domain, work.allocations[i], 0, sizes[i], MGMEM_WRITE, &work.borrows[i]));
        }
        input.read(static_cast<char *>(work.borrows[0].data), static_cast<std::streamsize>(rgb_bytes));
        require(static_cast<uint64_t>(input.gcount()) == rgb_bytes && input.peek() == EOF, "truncated input or trailing bytes");
        check(mgmem_borrow_release_v1(work.domain, work.borrows[0].lease));
        work.borrows[0] = {};
        check(mgmem_borrow_acquire_v1(work.domain, work.allocations[0], 0, rgb_bytes, MGMEM_READ, &work.borrows[0]));

        // Leases are obtained before accepting work. Owner close does not invalidate
        // the raw pointers; the worker releases each lease after its last access.
        std::promise<void> accepted;
        auto start = accepted.get_future();
        auto operation = std::packaged_task<uint64_t()>([&] {
            start.get();
            const auto *rgb = static_cast<const uint8_t *>(work.borrows[0].data);
            auto *luma = static_cast<uint8_t *>(work.borrows[1].data);
            auto *blur = static_cast<uint8_t *>(work.borrows[2].data);
            for (uint64_t i = 0; i < pixels; ++i)
                luma[i] = static_cast<uint8_t>((77u * rgb[3*i] + 150u * rgb[3*i+1] + 29u * rgb[3*i+2]) >> 8);
            uint64_t checksum = 0;
            for (uint64_t y = 0; y < height; ++y) for (uint64_t x = 0; x < width; ++x) {
                unsigned sum = 0;
                for (int dy = -1; dy <= 1; ++dy) for (int dx = -1; dx <= 1; ++dx) {
                    const auto yy = dy < 0 ? (y ? y-1 : 0) : (dy > 0 && y+1 < height ? y+1 : y);
                    const auto xx = dx < 0 ? (x ? x-1 : 0) : (dx > 0 && x+1 < width ? x+1 : x);
                    sum += luma[yy*width+xx];
                }
                blur[y*width+x] = static_cast<uint8_t>(sum / 9);
                checksum += blur[y*width+x];
            }
            std::ofstream output(argv[2], std::ios::binary);
            require(output.is_open(), "cannot open output");
            output << "P5\n" << width << ' ' << height << "\n255\n";
            output.write(reinterpret_cast<const char *>(blur), static_cast<std::streamsize>(pixels));
            output.close();
            require(output.good(), "failed output write");
            for (auto &borrow : work.borrows) {
                check(mgmem_borrow_release_v1(work.domain, borrow.lease));
                borrow = {};
            }
            return checksum;
        });
        auto result = operation.get_future();
        // Complete all throwable admission checks before starting the worker.
        for (auto &owner : work.allocations) { check(mgmem_release_v1(work.domain, owner)); owner = {}; }
        check(mgmem_domain_begin_close_v1(work.domain));
        require(mgmem_domain_collect_v1(work.domain) == MGMEM_BUSY &&
                mgmem_domain_drain_v1(work.domain, 0) == MGMEM_TIMEOUT, "close released active worker storage");
        std::thread worker(std::move(operation));
        accepted.set_value();
        worker.join();
        const uint64_t checksum = result.get();
        check(mgmem_domain_collect_v1(work.domain));
        check(mgmem_domain_drain_v1(work.domain, 0));
        mgmem_snapshot_v1 finished{};
        check(mgmem_domain_snapshot_v1(work.domain, &finished, sizeof(finished)));
        require(finished.backing_bytes == 0 && finished.active_leases == 0, "workspace did not reclaim");
        check(mgmem_domain_destroy_v1(work.domain));
        work.domain = nullptr;
        std::cout << "{\"consumer\":\"ppm_luma_box_blur\",\"success\":true,\"mode\":\""
                  << (mode == MGMEM_ARENA ? "arena" : "system") << "\",\"pixels\":" << pixels
                  << ",\"checksum\":" << checksum << ",\"peak_backing_bytes\":" << finished.peak_backing_bytes
                  << ",\"allocation_count\":" << finished.allocation_count << ",\"provider_allocation_count\":"
                  << finished.provider_allocation_count << ",\"closed_backing_bytes\":0,\"late_worker_close_safe\":true}\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "Image consumer: " << error.what() << '\n';
        return 1;
    }
}
