"""A waiting request must not bypass its stage timeout or kill another request."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'upstream/backend/main/xiaozhi-server'))
from core.providers.local_worker import ModelWorker


class WorkerBudgetTest(unittest.TestCase):
    def test_queue_wait_is_bounded_without_starting_or_stopping_worker(self):
        worker=ModelWorker()
        errors=[]
        def request():
            try:
                worker.call('asr',b'',{'timeout_s':.03})
            except Exception as exc:
                errors.append(exc)
        with patch('core.providers.local_worker.mp.get_context',side_effect=AssertionError('must not spawn')), \
                patch.object(worker,'close') as close:
            worker.lock.acquire()
            thread=threading.Thread(target=request,daemon=True)
            thread.start()
            try:
                thread.join(.5)
                self.assertFalse(thread.is_alive(),'stage waited beyond its budget')
                self.assertEqual(len(errors),1)
                self.assertIsInstance(errors[0],TimeoutError)
                self.assertIn('asr_queue_timeout',str(errors[0]))
                close.assert_not_called()
            finally:
                worker.lock.release()
                thread.join(1)
