#include <mgmem/mgmem.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdlib>
#include <iterator>
#include <limits>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <new>
#include <utility>

#if defined(_WIN32)
#include <malloc.h>
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace {
using u64 = uint64_t;
constexpr u64 max_u64 = std::numeric_limits<u64>::max();
std::atomic<u64> next_domain_id{1};
unsigned char runtime_anchor;
u64 runtime_origin() noexcept { return static_cast<u64>(reinterpret_cast<uintptr_t>(&runtime_anchor)); }

bool add(u64 a, u64 b, u64 &out) noexcept {
    if (a > max_u64 - b) return false;
    out = a + b;
    return true;
}

bool power_of_two(u64 n) noexcept { return n != 0 && (n & (n - 1)) == 0; }

bool round_up(u64 n, u64 alignment, u64 &out) noexcept {
    u64 sum = 0;
    if (!power_of_two(alignment) || !add(n, alignment - 1, sum)) return false;
    out = sum & ~(alignment - 1);
    return true;
}

bool take_domain_id(u64 &id) noexcept {
    u64 candidate = next_domain_id.load(std::memory_order_relaxed);
    while (candidate != max_u64) {
        if (next_domain_id.compare_exchange_weak(candidate, candidate + 1,
                std::memory_order_relaxed)) {
            id = candidate;
            return true;
        }
    }
    return false;
}

u64 page_size() noexcept {
#if defined(_WIN32)
    SYSTEM_INFO info{};
    GetSystemInfo(&info);
    return info.dwPageSize;
#else
    const long size = sysconf(_SC_PAGESIZE);
    return size > 0 ? static_cast<u64>(size) : 0;
#endif
}

template<class Function>
mgmem_status guarded(Function &&function) noexcept {
    try {
        return function();
    } catch (const std::bad_alloc &) {
        return MGMEM_OUT_OF_MEMORY;
    } catch (...) {
        return MGMEM_PROVIDER_ERROR;
    }
}

struct Backing {
    void *address = nullptr;
    u64 bytes = 0;
    u64 alignment = 0;
    u64 used = 0;
    bool pending = false;
};

struct Allocation {
    Backing *backing = nullptr;
    u64 offset = 0;
    u64 bytes = 0;
    u64 readers = 0;
    bool writer = false;
    bool owner = false;
    bool pending = false;
};

struct Lease {
    u64 allocation = 0;
    bool writer = false;
};
} // namespace

struct mgmem_domain {
    std::mutex mutex;
    std::condition_variable changed;
    uint32_t state = MGMEM_OPEN;
    uint32_t mode = MGMEM_SYSTEM;
    u64 id = 0;
    u64 next_id = 1;
    u64 generation = 1;
    u64 budget = 0;
    u64 chunk_bytes = 0;
    u64 page = 0;
    u64 backing_bytes = 0;
    u64 reserved_bytes = 0;
    u64 peak_bytes = 0;
    u64 live_bytes = 0;
    u64 retiring_bytes = 0;
    u64 allocation_count = 0;
    u64 provider_allocation_count = 0;
    u64 cumulative_requested = 0;
    u64 pending = 0;
    bool maintenance = false;
    bool provider_poisoned = false;
    bool custom_provider = false;
    mgmem_provider_v1 provider{};
    std::list<Backing> backing;
    std::map<u64, Allocation> allocations;
    std::map<u64, Lease> leases;
};

namespace {
mgmem_status allocate_backing(mgmem_domain &domain, u64 bytes, u64 alignment,
                              void **out) noexcept {
    *out = nullptr;
    if (domain.custom_provider) {
        try {
            return domain.provider.allocate(domain.provider.context, bytes, alignment, out);
        } catch (const std::bad_alloc &) {
            return MGMEM_OUT_OF_MEMORY;
        } catch (...) {
            return MGMEM_PROVIDER_ERROR;
        }
    }
#if defined(_WIN32)
    *out = _aligned_malloc(static_cast<size_t>(bytes), static_cast<size_t>(alignment));
#else
    if (posix_memalign(out, static_cast<size_t>(alignment), static_cast<size_t>(bytes)) != 0)
        *out = nullptr;
#endif
    return *out ? MGMEM_OK : MGMEM_OUT_OF_MEMORY;
}

bool deallocate_backing(mgmem_domain &domain, const Backing &backing) noexcept {
    if (!backing.address) return true;
    if (domain.custom_provider) {
        try {
            domain.provider.deallocate(domain.provider.context, backing.address,
                                       backing.bytes, backing.alignment);
        } catch (...) {
            // A callback violating noexcept might already have freed the address.
            // Do not retry it and risk a double free; this domain becomes poisoned.
            return false;
        }
    } else {
#if defined(_WIN32)
        _aligned_free(backing.address);
#else
        std::free(backing.address);
#endif
    }
    return true;
}

bool take_id(mgmem_domain &domain, u64 &id) noexcept {
    if (domain.next_id == max_u64) return false;
    id = domain.next_id++;
    return true;
}

bool valid_handle(const mgmem_domain &domain, mgmem_handle_v1 handle) noexcept {
    return handle.runtime_origin == runtime_origin() && handle.domain_id == domain.id && handle.id != 0;
}

bool quiet(const mgmem_domain &domain) noexcept {
    return domain.allocations.empty() && domain.leases.empty() && domain.pending == 0 &&
           !domain.maintenance;
}

bool counters_fit(const mgmem_domain &domain, u64 bytes, bool new_backing) noexcept {
    u64 ignored = 0;
    return domain.allocation_count != max_u64 &&
           (!new_backing || domain.provider_allocation_count != max_u64) &&
           add(domain.cumulative_requested, bytes, ignored) &&
           add(domain.live_bytes, bytes, ignored);
}

void commit_allocation(mgmem_domain &domain, Allocation &allocation, u64 bytes) noexcept {
    allocation.owner = true;
    allocation.pending = false;
    domain.live_bytes += bytes;
    ++domain.allocation_count;
    domain.cumulative_requested += bytes;
}

// The allocation metadata has already been consumed. Keep backing charged until
// deallocation finishes; callback latency must not let another request evade budget.
mgmem_status reclaim_system(mgmem_domain &domain, Backing *backing,
                            std::unique_lock<std::mutex> &lock) noexcept {
    std::list<Backing> detached;
    for (auto it = domain.backing.begin(); it != domain.backing.end(); ++it) {
        if (&*it == backing) {
            detached.splice(detached.end(), domain.backing, it);
            break;
        }
    }
    if (detached.empty()) {
        domain.provider_poisoned = true;
        return MGMEM_PROVIDER_ERROR;
    }
    ++domain.pending;
    lock.unlock();
    const bool success = deallocate_backing(domain, detached.front());
    lock.lock();
    if (success) domain.backing_bytes -= detached.front().bytes;
    else domain.provider_poisoned = true;
    --domain.pending;
    domain.changed.notify_all();
    return success ? MGMEM_OK : MGMEM_PROVIDER_ERROR;
}

// No allocations or leases exist when entering. Splicing is allocation-free.
// While callbacks run, maintenance blocks new allocation/reset/trim/drain.
mgmem_status reclaim_all(mgmem_domain &domain, std::unique_lock<std::mutex> &lock,
                         bool closing) {
    if (domain.provider_poisoned) return MGMEM_PROVIDER_ERROR;
    std::list<Backing> detached;
    detached.splice(detached.end(), domain.backing);
    domain.maintenance = true;
    ++domain.pending;
    lock.unlock();
    u64 released = 0;
    bool success = true;
    for (const auto &backing : detached) {
        if (deallocate_backing(domain, backing)) released += backing.bytes;
        else success = false;
    }
    lock.lock();
    domain.backing_bytes -= released;
    --domain.pending;
    domain.maintenance = false;
    domain.provider_poisoned = !success;
    if (success && closing) domain.state = MGMEM_CLOSED;
    domain.changed.notify_all();
    return success ? MGMEM_OK : MGMEM_PROVIDER_ERROR;
}
} // namespace

extern "C" {
uint32_t mgmem_abi_version_v1(void) { return (MGMEM_ABI_MAJOR << 16) | MGMEM_ABI_MINOR; }
uint64_t mgmem_capabilities_v1(void) {
    return MGMEM_CAP_SYSTEM | MGMEM_CAP_ARENA | MGMEM_CAP_CUSTOM_PROVIDER |
           MGMEM_CAP_RW_LEASE | MGMEM_CAP_EXPLICIT_COLLECTION;
}
uint64_t mgmem_runtime_origin_v1(void) { return runtime_origin(); }

mgmem_status mgmem_checked_mul_v1(u64 a, u64 b, u64 *out) {
    if (!out) return MGMEM_INVALID_ARGUMENT;
    *out = 0;
    if (b != 0 && a > max_u64 / b) return MGMEM_OVERFLOW;
    *out = a * b;
    return MGMEM_OK;
}

mgmem_status mgmem_checked_round_up_v1(u64 bytes, u64 alignment, u64 *out) {
    if (!out) return MGMEM_INVALID_ARGUMENT;
    *out = 0;
    if (!power_of_two(alignment)) return MGMEM_INVALID_ARGUMENT;
    return round_up(bytes, alignment, *out) ? MGMEM_OK : MGMEM_OVERFLOW;
}

mgmem_status mgmem_domain_create_v1(const mgmem_domain_options_v1 *options,
                                    mgmem_domain **out) {
    if (!out) return MGMEM_INVALID_ARGUMENT;
    *out = nullptr;
    return guarded([&]() -> mgmem_status {
        if (!options || options->struct_size < sizeof(*options)) return MGMEM_INVALID_ARGUMENT;
        if (options->abi_major != MGMEM_ABI_MAJOR || options->abi_minor > MGMEM_ABI_MINOR ||
            options->required_flags != 0) return MGMEM_UNSUPPORTED;
        if (options->reserved != 0 || options->mode > MGMEM_ARENA || options->budget_bytes == 0)
            return MGMEM_INVALID_ARGUMENT;
        if (options->mode == MGMEM_ARENA && options->arena_chunk_bytes == 0)
            return MGMEM_INVALID_ARGUMENT;
        if (options->arena_chunk_bytes > std::numeric_limits<size_t>::max()) return MGMEM_OVERFLOW;
        if (options->provider) {
            const auto &provider = *options->provider;
            if (provider.struct_size < sizeof(provider)) return MGMEM_INVALID_ARGUMENT;
            if (provider.abi_major != MGMEM_ABI_MAJOR || provider.abi_minor > MGMEM_ABI_MINOR ||
                provider.required_flags != 0) return MGMEM_UNSUPPORTED;
            if (!provider.allocate || !provider.deallocate || !provider.retain || !provider.release)
                return MGMEM_INVALID_ARGUMENT;
        }
        auto domain = std::make_unique<mgmem_domain>();
        if (!take_domain_id(domain->id)) return MGMEM_OVERFLOW;
        domain->mode = options->mode;
        domain->budget = options->budget_bytes;
        domain->chunk_bytes = options->arena_chunk_bytes;
        domain->page = page_size();
        if (options->provider) {
            domain->provider = *options->provider;
            domain->custom_provider = true;
            domain->provider.retain(domain->provider.context);
        }
        *out = domain.release();
        return MGMEM_OK;
    });
}

mgmem_status mgmem_allocate_v1(mgmem_domain *domain, u64 bytes, u64 alignment,
                               mgmem_handle_v1 *out) {
    if (!out) return MGMEM_INVALID_ARGUMENT;
    *out = {};
    if (!domain || bytes == 0 || !power_of_two(alignment)) return MGMEM_INVALID_ARGUMENT;
    if (bytes > std::numeric_limits<size_t>::max() ||
        alignment > std::numeric_limits<size_t>::max()) return MGMEM_OVERFLOW;
    alignment = std::max(alignment, static_cast<u64>(sizeof(void *)));
    return guarded([&]() -> mgmem_status {
        std::unique_lock<std::mutex> lock(domain->mutex);
        if (domain->state != MGMEM_OPEN) return MGMEM_IS_CLOSED;
        if (domain->provider_poisoned) return MGMEM_PROVIDER_ERROR;
        if (domain->maintenance) return MGMEM_BUSY;
        if (!counters_fit(*domain, bytes, false)) return MGMEM_OVERFLOW;
        u64 id = 0;
        if (!take_id(*domain, id)) return MGMEM_OVERFLOW;
        if (domain->mode == MGMEM_ARENA) {
            for (auto &backing : domain->backing) {
                if (backing.pending || backing.alignment < alignment) continue;
                u64 offset = 0;
                if (!round_up(backing.used, alignment, offset) || offset > backing.bytes ||
                    bytes > backing.bytes - offset) continue;
                auto insertion = domain->allocations.emplace(id,
                    Allocation{&backing, offset, bytes, 0, false, false, false});
                commit_allocation(*domain, insertion.first->second, bytes);
                backing.used = offset + bytes;
                *out = {domain->id, id, runtime_origin()};
                return MGMEM_OK;
            }
        }
        if (!counters_fit(*domain, bytes, true)) return MGMEM_OVERFLOW;
        const u64 capacity = domain->mode == MGMEM_ARENA ? std::max(bytes, domain->chunk_bytes) : bytes;
        if (capacity > std::numeric_limits<size_t>::max()) return MGMEM_OVERFLOW;
        u64 committed_and_pending = 0;
        u64 total = 0;
        if (!add(domain->backing_bytes, domain->reserved_bytes, committed_and_pending) ||
            !add(committed_and_pending, capacity, total)) return MGMEM_OVERFLOW;
        if (total > domain->budget) return MGMEM_OUT_OF_BUDGET;
        if (domain->pending == max_u64) return MGMEM_OVERFLOW;

        // Allocate metadata before accepting the provider operation. A throwing
        // map insertion removes the placeholder and never reserves any budget.
        domain->backing.emplace_back(Backing{nullptr, capacity, alignment, 0, true});
        auto backing_it = std::prev(domain->backing.end());
        try {
            domain->allocations.emplace(id,
                Allocation{&*backing_it, 0, bytes, 0, false, false, true});
        } catch (...) {
            domain->backing.erase(backing_it);
            throw;
        }
        domain->reserved_bytes += capacity;
        domain->peak_bytes = std::max(domain->peak_bytes, total);
        ++domain->pending;
        lock.unlock();
        void *address = nullptr;
        mgmem_status status = allocate_backing(*domain, capacity, alignment, &address);
        const bool aligned = address && (reinterpret_cast<uintptr_t>(address) & (alignment - 1)) == 0;
        if (status == MGMEM_OK && !aligned) status = MGMEM_PROVIDER_ERROR;
        lock.lock();
        // Counter headroom can have changed while a different callback was in
        // flight. Recheck before publication; rollback leaves old allocations live.
        if (status == MGMEM_OK && !counters_fit(*domain, bytes, true)) status = MGMEM_OVERFLOW;
        if (status == MGMEM_OK && domain->state != MGMEM_OPEN) status = MGMEM_IS_CLOSED;
        if (status == MGMEM_OK && domain->provider_poisoned) status = MGMEM_PROVIDER_ERROR;
        backing_it->address = address;
        if (status == MGMEM_OK) {
            backing_it->pending = false;
            backing_it->used = bytes;
            domain->reserved_bytes -= capacity;
            domain->backing_bytes += capacity;
            ++domain->provider_allocation_count;
            --domain->pending;
            auto &allocation = domain->allocations.find(id)->second;
            commit_allocation(*domain, allocation, bytes);
            *out = {domain->id, id, runtime_origin()};
            domain->changed.notify_all();
            return MGMEM_OK;
        }
        // Failure or close raced with admission. Reservation stays charged and
        // pending keeps the domain/provider alive through rollback deallocation.
        const Backing failed = *backing_it;
        domain->allocations.erase(id);
        domain->backing.erase(backing_it);
        lock.unlock();
        const bool reclaimed = deallocate_backing(*domain, failed);
        lock.lock();
        domain->reserved_bytes -= capacity;
        --domain->pending;
        if (!reclaimed) {
            domain->provider_poisoned = true;
            domain->backing_bytes += capacity;
            status = MGMEM_PROVIDER_ERROR;
        }
        domain->changed.notify_all();
        return status;
    });
}

mgmem_status mgmem_release_v1(mgmem_domain *domain, mgmem_handle_v1 handle) {
    if (!domain) return MGMEM_INVALID_ARGUMENT;
    return guarded([&]() -> mgmem_status {
        std::unique_lock<std::mutex> lock(domain->mutex);
        if (!valid_handle(*domain, handle)) return MGMEM_STALE_HANDLE;
        auto found = domain->allocations.find(handle.id);
        if (found == domain->allocations.end() || !found->second.owner || found->second.pending)
            return MGMEM_STALE_HANDLE;
        auto &allocation = found->second;
        allocation.owner = false;
        domain->live_bytes -= allocation.bytes;
        if (allocation.readers != 0 || allocation.writer) {
            domain->retiring_bytes += allocation.bytes;
            return MGMEM_OK;
        }
        Backing *backing = allocation.backing;
        domain->allocations.erase(found);
        domain->changed.notify_all();
        return domain->mode == MGMEM_SYSTEM ? reclaim_system(*domain, backing, lock) : MGMEM_OK;
    });
}

mgmem_status mgmem_borrow_acquire_v1(mgmem_domain *domain, mgmem_handle_v1 handle,
    u64 offset, u64 length, uint32_t access, mgmem_borrow_v1 *out) {
    if (!out) return MGMEM_INVALID_ARGUMENT;
    *out = {};
    if (!domain || length == 0 || access > MGMEM_WRITE) return MGMEM_INVALID_ARGUMENT;
    return guarded([&]() -> mgmem_status {
        std::lock_guard<std::mutex> lock(domain->mutex);
        if (domain->state != MGMEM_OPEN) return MGMEM_IS_CLOSED;
        if (!valid_handle(*domain, handle)) return MGMEM_STALE_HANDLE;
        auto found = domain->allocations.find(handle.id);
        if (found == domain->allocations.end() || !found->second.owner || found->second.pending)
            return MGMEM_STALE_HANDLE;
        auto &allocation = found->second;
        if (offset > allocation.bytes || length > allocation.bytes - offset)
            return MGMEM_INVALID_ARGUMENT;
        if (allocation.writer || (access == MGMEM_WRITE && allocation.readers != 0)) return MGMEM_BUSY;
        if (allocation.readers == max_u64) return MGMEM_OVERFLOW;
        u64 id = 0;
        if (!take_id(*domain, id)) return MGMEM_OVERFLOW;
        domain->leases.emplace(id, Lease{handle.id, access == MGMEM_WRITE});
        if (access == MGMEM_WRITE) allocation.writer = true;
        else ++allocation.readers;
        out->lease = {domain->id, id, runtime_origin()};
        out->data = static_cast<unsigned char *>(allocation.backing->address) + allocation.offset + offset;
        out->length = length;
        return MGMEM_OK;
    });
}

mgmem_status mgmem_borrow_release_v1(mgmem_domain *domain, mgmem_handle_v1 handle) {
    if (!domain) return MGMEM_INVALID_ARGUMENT;
    return guarded([&]() -> mgmem_status {
        std::unique_lock<std::mutex> lock(domain->mutex);
        if (!valid_handle(*domain, handle)) return MGMEM_STALE_HANDLE;
        const auto lease = domain->leases.find(handle.id);
        if (lease == domain->leases.end()) return MGMEM_STALE_HANDLE;
        const auto found = domain->allocations.find(lease->second.allocation);
        if (found == domain->allocations.end()) return MGMEM_PROVIDER_ERROR;
        auto &allocation = found->second;
        if (lease->second.writer) allocation.writer = false;
        else --allocation.readers;
        domain->leases.erase(lease);
        if (!allocation.owner && allocation.readers == 0 && !allocation.writer) {
            Backing *backing = allocation.backing;
            domain->retiring_bytes -= allocation.bytes;
            domain->allocations.erase(found);
            domain->changed.notify_all();
            return domain->mode == MGMEM_SYSTEM ? reclaim_system(*domain, backing, lock) : MGMEM_OK;
        }
        domain->changed.notify_all();
        return MGMEM_OK;
    });
}

mgmem_status mgmem_arena_reset_v1(mgmem_domain *domain) {
    if (!domain) return MGMEM_INVALID_ARGUMENT;
    return guarded([&]() -> mgmem_status {
        std::lock_guard<std::mutex> lock(domain->mutex);
        if (domain->state != MGMEM_OPEN) return MGMEM_IS_CLOSED;
        if (domain->mode != MGMEM_ARENA) return MGMEM_UNSUPPORTED;
        if (domain->provider_poisoned) return MGMEM_PROVIDER_ERROR;
        if (!quiet(*domain)) return MGMEM_BUSY;
        if (domain->generation == max_u64) return MGMEM_OVERFLOW;
        for (auto &backing : domain->backing) backing.used = 0;
        ++domain->generation;
        return MGMEM_OK;
    });
}

mgmem_status mgmem_domain_trim_v1(mgmem_domain *domain) {
    if (!domain) return MGMEM_INVALID_ARGUMENT;
    return guarded([&]() -> mgmem_status {
        std::unique_lock<std::mutex> lock(domain->mutex);
        if (domain->state != MGMEM_OPEN) return MGMEM_IS_CLOSED;
        if (!quiet(*domain)) return MGMEM_BUSY;
        return reclaim_all(*domain, lock, false);
    });
}

mgmem_status mgmem_domain_begin_close_v1(mgmem_domain *domain) {
    if (!domain) return MGMEM_INVALID_ARGUMENT;
    return guarded([&]() -> mgmem_status {
        std::lock_guard<std::mutex> lock(domain->mutex);
        if (domain->state == MGMEM_OPEN) domain->state = MGMEM_CLOSING;
        domain->changed.notify_all();
        return MGMEM_OK;
    });
}

mgmem_status mgmem_domain_drain_v1(mgmem_domain *domain, u64 timeout_ms) {
    if (!domain) return MGMEM_INVALID_ARGUMENT;
    using milliseconds = std::chrono::milliseconds;
    using clock = std::chrono::steady_clock;
    // Bound both the duration conversion and deadline addition.
    const auto maximum = std::chrono::duration_cast<milliseconds>(clock::duration::max() / 4).count();
    if (timeout_ms > static_cast<u64>(maximum)) return MGMEM_OVERFLOW;
    return guarded([&]() -> mgmem_status {
        std::unique_lock<std::mutex> lock(domain->mutex);
        if (domain->state == MGMEM_OPEN) return MGMEM_INVALID_ARGUMENT;
        if (domain->state == MGMEM_CLOSED) return MGMEM_OK;
        if (domain->provider_poisoned) return MGMEM_PROVIDER_ERROR;
        if (timeout_ms == 0) return MGMEM_TIMEOUT;
        const auto deadline = clock::now() + milliseconds(timeout_ms);
        if (!domain->changed.wait_until(lock, deadline, [&] {
                return domain->state == MGMEM_CLOSED || domain->provider_poisoned;
            })) return MGMEM_TIMEOUT;
        if (domain->provider_poisoned) return MGMEM_PROVIDER_ERROR;
        return MGMEM_OK;
    });
}

mgmem_status mgmem_domain_collect_v1(mgmem_domain *domain) {
    if (!domain) return MGMEM_INVALID_ARGUMENT;
    return guarded([&]() -> mgmem_status {
        std::unique_lock<std::mutex> lock(domain->mutex);
        if (domain->state == MGMEM_OPEN) return MGMEM_INVALID_ARGUMENT;
        if (domain->state == MGMEM_CLOSED) return MGMEM_OK;
        if (!quiet(*domain)) return MGMEM_BUSY;
        return reclaim_all(*domain, lock, true);
    });
}

mgmem_status mgmem_domain_destroy_v1(mgmem_domain *domain) {
    if (!domain) return MGMEM_INVALID_ARGUMENT;
    return guarded([&]() -> mgmem_status {
        {
            std::lock_guard<std::mutex> lock(domain->mutex);
            if (domain->state != MGMEM_CLOSED || !quiet(*domain) || domain->backing_bytes != 0)
                return MGMEM_BUSY;
        }
        // Caller serializes destruction with all other APIs; callbacks stay outside
        // the lock and the provider context survives every backing release.
        if (domain->custom_provider) {
            const auto provider = domain->provider;
            domain->custom_provider = false;
            try {
                provider.release(provider.context);
            } catch (...) {
                delete domain;
                return MGMEM_PROVIDER_ERROR;
            }
        }
        delete domain;
        return MGMEM_OK;
    });
}

mgmem_status mgmem_domain_snapshot_v1(mgmem_domain *domain, mgmem_snapshot_v1 *out,
                                      uint32_t out_size) {
    if (!domain || !out || out_size < sizeof(*out)) return MGMEM_INVALID_ARGUMENT;
    return guarded([&]() -> mgmem_status {
        std::lock_guard<std::mutex> lock(domain->mutex);
        mgmem_snapshot_v1 result{};
        result.struct_size = sizeof(result);
        result.abi_major = MGMEM_ABI_MAJOR;
        result.abi_minor = MGMEM_ABI_MINOR;
        result.state = domain->state;
        result.domain_id = domain->id;
        result.generation = domain->generation;
        result.budget_bytes = domain->budget;
        result.backing_bytes = domain->backing_bytes;
        result.reserved_bytes = domain->reserved_bytes;
        result.peak_backing_bytes = domain->peak_bytes;
        result.live_bytes = domain->live_bytes;
        result.retiring_bytes = domain->retiring_bytes;
        result.retained_bytes = domain->backing_bytes - domain->live_bytes - domain->retiring_bytes;
        result.allocation_count = domain->allocation_count;
        result.provider_allocation_count = domain->provider_allocation_count;
        result.cumulative_requested_bytes = domain->cumulative_requested;
        result.active_allocations = domain->allocations.size();
        result.active_leases = domain->leases.size();
        result.page_size = domain->page;
        *out = result;
        return MGMEM_OK;
    });
}
} // extern "C"
