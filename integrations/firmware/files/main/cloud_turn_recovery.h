#ifndef CLOUD_TURN_RECOVERY_H
#define CLOUD_TURN_RECOVERY_H

#include <algorithm>
#include <cmath>
#include <cstdint>

// Owned by the application task. A failed turn delays new automatic captures;
// it neither changes the user's listening preference nor resends old audio.
class CloudTurnRecovery {
public:
    int PauseAfterError(int64_t now_us, bool delay_is_number, double delay_ms) {
        int delay = 3000;
        if (delay_is_number && std::isfinite(delay_ms) && delay_ms >= 1 && delay_ms <= 86400000 &&
            std::floor(delay_ms) == delay_ms) {
            delay = std::max(delay, static_cast<int>(delay_ms));
        }
        retry_at_us_ = std::max(retry_at_us_, now_us + static_cast<int64_t>(delay) * 1000);
        return delay;
    }

    bool CanListen(int64_t now_us) const { return now_us >= retry_at_us_; }
    void Clear() { retry_at_us_ = 0; }

private:
    int64_t retry_at_us_ = 0;
};

#endif
