"""Exercise the production warmup gate with logical pauses and input restarts."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class InputWarmupTest(unittest.TestCase):
    def test_ready_capture_resumes_but_restarted_input_must_settle(self):
        compiler = shutil.which("c++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            source = tmp / "test.cc"
            source.write_text(r'''
#include <cassert>
#include <future>
#include <thread>
#include "input_warmup.h"

int main() {
    InputWarmup gate;
    gate.OnInputEnabled();
    // Cold input still needs all five seconds after its first processed audio.
    assert(!gate.IsReady(0));
    assert(!gate.IsReady(4999999));
    assert(gate.IsReady(5000000));

    // Capture pauses during a reply do not change the physical input cycle.
    assert(gate.IsReady(5100000));
    assert(gate.IsReady(8500000));
    assert(gate.IsReady(9000000));

    // Use a different thread just as the input task reports a real restart to
    // the AFE task. A previously ready microphone must become cold again.
    std::promise<void> restarted;
    auto observed = restarted.get_future();
    std::thread input([&]() {
        gate.OnInputEnabled();
        restarted.set_value();
    });
    observed.wait();
    assert(!gate.IsReady(10000000));
    assert(!gate.IsReady(14999999));
    assert(gate.IsReady(15000000));
    input.join();

    // Multiple restarts without any processed audio cannot spend the warmup
    // allowance while silent. Count from the first new processed frame.
    gate.OnInputEnabled();
    gate.OnInputEnabled();
    assert(!gate.IsReady(30000000));
    assert(!gate.IsReady(34999999));
    assert(gate.IsReady(35000000));

    // Pausing before stability cannot bypass the original five-second gate.
    gate.OnInputEnabled();
    assert(!gate.IsReady(40000000));
    assert(!gate.IsReady(41000000));
    assert(!gate.IsReady(44000000));
    assert(gate.IsReady(45000000));
}
''')
            binary = tmp / "input-warmup-test"
            subprocess.run([compiler, "-std=c++17", "-pthread", "-Wall", "-Wextra",
                            "-Werror", "-I", str(ROOT / "main/audio/engines"),
                            str(source), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
