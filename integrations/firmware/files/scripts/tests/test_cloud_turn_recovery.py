"""Run production automatic-listening retry policy on the host."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CloudTurnRecoveryTest(unittest.TestCase):
    def test_error_delays_resume_and_explicit_control_can_clear_them(self):
        compiler = shutil.which("c++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            source = tmp / "test.cc"
            source.write_text(r'''
#include <cassert>
#include <limits>
#include "cloud_turn_recovery.h"

int main() {
    CloudTurnRecovery retry;
    assert(retry.CanListen(0));
    assert(retry.PauseAfterError(1000000, false, 0) == 3000);
    // A following tts:stop/ordinary 300 ms rearm cannot erase this error delay.
    assert(!retry.CanListen(1300000));
    assert(!retry.CanListen(3999999));
    assert(retry.CanListen(4000000));

    // Budget/access pauses suppress fresh automatic captures, while explicit
    // BOOT off/on can recover immediately after the owner adjusts the budget.
    assert(retry.PauseAfterError(5000000, true, 86400000) == 86400000);
    assert(!retry.CanListen(86404999999LL));
    assert(retry.CanListen(86405000000LL));
    retry.PauseAfterError(5000001, true, 3000);
    assert(!retry.CanListen(10000000)); // A later short delay cannot undo a long one.
    retry.Clear();
    assert(retry.CanListen(5000002));

    assert(retry.PauseAfterError(20000000, true, 60000) == 60000);
    assert(!retry.CanListen(79999999));
    assert(retry.CanListen(80000000));
    retry.Clear();

    // Wrong-type/invalid numbers fall back to a bounded, nonzero delay. Tiny
    // valid values also cannot cause a loop of immediately retriggered turns.
    for (double value : {-1.0, 0.0, 3000.5, 86400001.0,
                          std::numeric_limits<double>::infinity(),
                          std::numeric_limits<double>::quiet_NaN()}) {
        assert(retry.PauseAfterError(0, true, value) == 3000);
        assert(!retry.CanListen(2999999));
        assert(retry.CanListen(3000000));
        retry.Clear();
    }
    assert(retry.PauseAfterError(0, true, 1) == 3000);
}
''')
            binary = tmp / "cloud-turn-recovery-test"
            subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "main"), str(source), "-o", str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
