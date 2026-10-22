import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cloud_budget import reserve, positive

class BudgetTests(unittest.TestCase):
    def test_restart_errors_stay_reserved_and_day_rollover(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'budget.sqlite'
            reserve(path,'.06','.1',3,'2026-10-08')
            with self.assertRaisesRegex(ValueError,'exhausted'):reserve(path,'.06','.1',3,'2026-10-08')
            self.assertEqual(reserve(path,'.06','.1',3,'2026-10-09')['daily_turns'],1)

    def test_concurrent_reservations_cannot_overrun(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'budget.sqlite'
            reserve(path,'.01','1',2,'2026-10-08')
            def call(i):
                try:reserve(path,'.01','1',2,'2026-10-08');return True
                except ValueError:return False
            with ThreadPoolExecutor(4) as pool:self.assertEqual(sum(pool.map(call,range(8))),1)

    def test_bad_limits_fail_closed(self):
        for value in ('','NaN','Infinity','-1','0'):
            with self.assertRaises(ValueError):positive(value,'test')
