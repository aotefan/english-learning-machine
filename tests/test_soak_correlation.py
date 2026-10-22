"""A prior request may finish logging after the client receives its stop frame."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from soak_backend import wait_for_event


class CorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_matching_request_not_previous_terminal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            path.write_text(json.dumps({'event': 'request_complete',
                                        'request_id': 'previous', 'success': True}) + '\n')

            async def finish_current():
                await asyncio.sleep(.02)
                with path.open('a') as out:
                    out.write(json.dumps({'event': 'request_complete',
                                          'request_id': 'current', 'success': False}) + '\n')

            writer = asyncio.create_task(finish_current())
            try:
                result = await wait_for_event(SimpleNamespace(events=path), 0,
                                              'request_complete', 'current')
                self.assertEqual(result['request_id'], 'current')
                self.assertIs(result['success'], False)
            finally:
                await writer
