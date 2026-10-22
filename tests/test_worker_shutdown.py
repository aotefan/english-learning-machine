"""Real spawned processes: cooperative exit, stuck child, and worker reuse."""
import multiprocessing as mp
import sys
import time
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'upstream/backend/main/xiaozhi-server'))
from core.providers.local_worker import ModelWorker


def stuck_child(pipe):
    pipe.send('ready')
    time.sleep(60)  # Simulate native inference that cannot consume a stop message.


class WorkerShutdownTests(unittest.TestCase):
    def setUp(self):
        self.worker=ModelWorker()
        self.addCleanup(lambda:self.worker.close(force=True))

    def exercise_real_dispatcher(self):
        # Load the real dispatcher but no large model; an unknown stage returns
        # a normal provider error and leaves the worker ready for another call.
        with self.assertRaisesRegex(RuntimeError,'Unknown stage'):
            self.worker.call('invalid',None,{'engine':'unused','model_path':'unused','timeout_s':5})
        return self.worker.process.pid

    def start_stuck_child(self):
        context=mp.get_context('spawn')
        parent,child=context.Pipe()
        process=context.Process(target=stuck_child,args=(child,),daemon=True)
        process.start();child.close()
        self.worker.process=process;self.worker.pipe=parent
        self.assertTrue(parent.poll(5),'test child failed to start')
        self.assertEqual(parent.recv(),'ready')

    def test_idle_dispatcher_exits_normally_and_closes_handles(self):
        pid=self.exercise_real_dispatcher()
        result=self.worker.close()
        self.assertEqual(result['pid'],pid)
        self.assertEqual(result['exit_code'],0)
        self.assertIs(result['forced'],False)
        self.assertIsNone(self.worker.process)
        self.assertIsNone(self.worker.pipe)
        self.assertIsNone(self.worker.close())

    def test_new_request_after_graceful_close_starts_new_child(self):
        first=self.exercise_real_dispatcher()
        self.assertEqual(self.worker.close()['exit_code'],0)
        second=self.exercise_real_dispatcher()
        self.assertNotEqual(first,second)
        self.assertEqual(self.worker.close()['exit_code'],0)

    def test_unresponsive_child_falls_back_to_termination(self):
        self.start_stuck_child()
        started=time.monotonic()
        result=self.worker.close()
        self.assertIs(result['forced'],True)
        self.assertLess(result['exit_code'],0)
        self.assertLess(time.monotonic()-started,5)

    def test_forced_timeout_path_does_not_wait_for_cooperative_exit(self):
        self.start_stuck_child()
        started=time.monotonic()
        result=self.worker.close(force=True)
        self.assertIs(result['forced'],True)
        self.assertLess(result['exit_code'],0)
        self.assertLess(time.monotonic()-started,1.5)
