#ifndef MGMEM_MEMORY_HPP
#define MGMEM_MEMORY_HPP

#include <mgmem/mgmem.h>

#include <exception>
#include <memory>
#include <new>
#include <utility>

/* Optional, header-only C++17 ownership adapter. No global allocator replacement.
 * Wrappers are consumer-local C++ objects, not a cross-DSO C++ ABI. The selected
 * native runtime module must remain loaded until every wrapper has been released.
 *
 * Each wrapper is move-only. Distinct wrappers may be handed between threads;
 * mutating the same wrapper concurrently still requires caller synchronization.
 * An allocation and every borrow retain the domain independently. Destroying a
 * domain wrapper never invalidates its surviving allocations or borrows.
 *
 * Storage is uninitialized. A raw data pointer may only be used while its borrow
 * exists, and READ pointers may not be written through const_cast. Acquire the
 * borrow before queueing asynchronous work. No raw C owner handle is exported.
 *
 * Successful release consumes the wrapper. Empty release is an idempotent OK.
 * All fallible creation functions require an empty output and preserve it on
 * failure. std::bad_alloc while creating shared ownership maps to OUT_OF_MEMORY.
 *
 * Final release/destruction may invoke synchronous provider cleanup, including
 * begin_close -> collect -> drain -> destroy for the last shared state. Arrange
 * for the last wrapper to be destroyed on a cleanup worker, not a UI/completion
 * thread that must remain responsive. begin_close itself remains nonblocking.
 *
 * Destructors are noexcept and require all C cleanup calls to succeed. A provider
 * violating its no-throw/lifetime contract or an invalid ownership state causes
 * std::terminate; resources are never silently leaked or forcibly freed while
 * still borrowed. Explicit release/collect return status for earlier diagnosis.
 */

namespace mgmem {
namespace detail {
inline void require_cleanup(mgmem_status status) noexcept {
    if (status != MGMEM_OK) std::terminate();
}

struct domain_state {
    mgmem_domain *native = nullptr;

    domain_state() = default;
    domain_state(const domain_state &) = delete;
    domain_state &operator=(const domain_state &) = delete;

    ~domain_state() noexcept {
        if (!native) return;
        // Every wrapped owner/lease holds this state. Its final destructor cannot
        // run while any such resource or admitted wrapper operation still exists.
        require_cleanup(mgmem_domain_begin_close_v1(native));
        require_cleanup(mgmem_domain_collect_v1(native));
        require_cleanup(mgmem_domain_drain_v1(native, 0));
        require_cleanup(mgmem_domain_destroy_v1(native));
    }
};
} // namespace detail

class domain;
class owned_allocation;

class borrow {
public:
    borrow() noexcept = default;
    borrow(const borrow &) = delete;
    borrow &operator=(const borrow &) = delete;

    borrow(borrow &&other) noexcept { take(other); }
    borrow &operator=(borrow &&other) noexcept {
        if (this != &other) {
            detail::require_cleanup(release());
            take(other);
        }
        return *this;
    }

    ~borrow() noexcept { detail::require_cleanup(release()); }

    explicit operator bool() const noexcept { return static_cast<bool>(state_); }
    const void *data() const noexcept { return view_.data; }
    void *write_data() const noexcept { return access_ == MGMEM_WRITE ? view_.data : nullptr; }
    uint64_t size_bytes() const noexcept { return view_.length; }
    uint32_t access() const noexcept { return access_; }

    mgmem_status release() noexcept {
        if (!state_) return MGMEM_OK;
        const mgmem_status status = mgmem_borrow_release_v1(state_->native, view_.lease);
        if (status == MGMEM_OK) {
            view_ = {};
            access_ = MGMEM_READ;
            state_.reset();
        }
        return status;
    }

private:
    friend class owned_allocation;
    std::shared_ptr<detail::domain_state> state_;
    mgmem_borrow_v1 view_{};
    uint32_t access_ = MGMEM_READ;

    void take(borrow &other) noexcept {
        state_ = std::move(other.state_);
        view_ = other.view_;
        access_ = other.access_;
        other.view_ = {};
        other.access_ = MGMEM_READ;
    }
};

class owned_allocation {
public:
    owned_allocation() noexcept = default;
    owned_allocation(const owned_allocation &) = delete;
    owned_allocation &operator=(const owned_allocation &) = delete;

    owned_allocation(owned_allocation &&other) noexcept { take(other); }
    owned_allocation &operator=(owned_allocation &&other) noexcept {
        if (this != &other) {
            detail::require_cleanup(release());
            take(other);
        }
        return *this;
    }

    ~owned_allocation() noexcept { detail::require_cleanup(release()); }

    explicit operator bool() const noexcept { return static_cast<bool>(state_); }
    uint64_t size_bytes() const noexcept { return bytes_; }

    mgmem_status acquire_borrow(uint64_t offset, uint64_t length, uint32_t access,
                                mgmem::borrow &out) const noexcept {
        if (!state_ || out) return MGMEM_INVALID_ARGUMENT;
        mgmem_borrow_v1 view{};
        const mgmem_status status = mgmem_borrow_acquire_v1(state_->native, handle_,
                                                           offset, length, access, &view);
        if (status != MGMEM_OK) return status;
        out.state_ = state_;
        out.view_ = view;
        out.access_ = access;
        return MGMEM_OK;
    }

    mgmem_status release() noexcept {
        if (!state_) return MGMEM_OK;
        const mgmem_status status = mgmem_release_v1(state_->native, handle_);
        if (status == MGMEM_OK) {
            handle_ = {};
            bytes_ = 0;
            state_.reset();
        }
        return status;
    }

private:
    friend class domain;
    std::shared_ptr<detail::domain_state> state_;
    mgmem_handle_v1 handle_{};
    uint64_t bytes_ = 0;

    void take(owned_allocation &other) noexcept {
        state_ = std::move(other.state_);
        handle_ = other.handle_;
        bytes_ = other.bytes_;
        other.handle_ = {};
        other.bytes_ = 0;
    }
};

class domain {
public:
    domain() noexcept = default;
    domain(const domain &) = delete;
    domain &operator=(const domain &) = delete;
    domain(domain &&) noexcept = default;
    domain &operator=(domain &&) noexcept = default;
    ~domain() noexcept = default;

    explicit operator bool() const noexcept { return static_cast<bool>(state_); }

    static mgmem_status create(const mgmem_domain_options_v1 &options, domain &out) noexcept {
        if (out) return MGMEM_INVALID_ARGUMENT;
        try {
            // Allocate C++ ownership before creating the C owner; OOM cannot orphan
            // a successfully created native domain.
            auto state = std::make_shared<detail::domain_state>();
            const mgmem_status status = mgmem_domain_create_v1(&options, &state->native);
            if (status != MGMEM_OK) return status;
            out.state_ = std::move(state);
            return MGMEM_OK;
        } catch (const std::bad_alloc &) {
            return MGMEM_OUT_OF_MEMORY;
        } catch (...) {
            return MGMEM_PROVIDER_ERROR;
        }
    }

    mgmem_status allocate(uint64_t bytes, uint64_t alignment, owned_allocation &out) const noexcept {
        if (!state_ || out) return MGMEM_INVALID_ARGUMENT;
        mgmem_handle_v1 handle{};
        const mgmem_status status = mgmem_allocate_v1(state_->native, bytes, alignment, &handle);
        if (status != MGMEM_OK) return status;
        out.state_ = state_;
        out.handle_ = handle;
        out.bytes_ = bytes;
        return MGMEM_OK;
    }

    mgmem_status import_external(const mgmem_external_buffer_v1 &buffer, owned_allocation &out) const noexcept {
        if (!state_ || out) return MGMEM_INVALID_ARGUMENT;
        mgmem_handle_v1 handle{};
        const mgmem_status status = mgmem_import_external_v1(state_->native, &buffer, &handle);
        if (status != MGMEM_OK) return status;
        out.state_ = state_;
        out.handle_ = handle;
        out.bytes_ = buffer.capacity_bytes;
        return MGMEM_OK;
    }

    mgmem_status set_budget(uint64_t bytes) const noexcept {
        return state_ ? mgmem_domain_set_budget_v1(state_->native, bytes) : MGMEM_INVALID_ARGUMENT;
    }

    mgmem_status import_snapshot(mgmem_import_snapshot_v1 &out) const noexcept {
        return state_ ? mgmem_domain_import_snapshot_v1(state_->native, &out, sizeof(out)) : MGMEM_INVALID_ARGUMENT;
    }

    mgmem_status begin_close() const noexcept {
        return state_ ? mgmem_domain_begin_close_v1(state_->native) : MGMEM_INVALID_ARGUMENT;
    }
    mgmem_status collect() const noexcept {
        return state_ ? mgmem_domain_collect_v1(state_->native) : MGMEM_INVALID_ARGUMENT;
    }
    mgmem_status drain(uint64_t timeout_ms) const noexcept {
        return state_ ? mgmem_domain_drain_v1(state_->native, timeout_ms) : MGMEM_INVALID_ARGUMENT;
    }
    mgmem_status reset() const noexcept {
        return state_ ? mgmem_arena_reset_v1(state_->native) : MGMEM_INVALID_ARGUMENT;
    }
    mgmem_status trim() const noexcept {
        return state_ ? mgmem_domain_trim_v1(state_->native) : MGMEM_INVALID_ARGUMENT;
    }
    mgmem_status snapshot(mgmem_snapshot_v1 &out) const noexcept {
        return state_ ? mgmem_domain_snapshot_v1(state_->native, &out, sizeof(out)) : MGMEM_INVALID_ARGUMENT;
    }

private:
    std::shared_ptr<detail::domain_state> state_;
};

} // namespace mgmem
#endif
