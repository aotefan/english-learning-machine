#ifndef INPUT_WARMUP_H
#define INPUT_WARMUP_H

#include <atomic>
#include <cstdint>

// The input task reports actual codec off/on cycles. Only the AFE processing
// task observes readiness; logical capture pauses must not restart this timer.
class InputWarmup {
public:
    void OnInputEnabled() { generation_.fetch_add(1); }

    bool IsReady(int64_t now_us) {
        const uint32_t generation = generation_.load();
        if (!observed_input_ || generation != observed_generation_) {
            observed_input_ = true;
            observed_generation_ = generation;
            first_audio_at_us_ = now_us;
        }
        // Begin with the first processed audio, not time spent with no samples.
        // A concurrent input restart cannot make an old frame appear warmed up.
        return now_us - first_audio_at_us_ >= 5000000 && generation_.load() == generation;
    }

private:
    std::atomic<uint32_t> generation_{0};
    uint32_t observed_generation_ = 0;
    bool observed_input_ = false;
    int64_t first_audio_at_us_ = 0;
};

#endif
