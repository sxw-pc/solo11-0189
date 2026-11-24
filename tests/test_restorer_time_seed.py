import json
import unittest
from pathlib import Path
from bookconserve.shift_capture import hours
from bookconserve.bench_capacity import remaining

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'restorer-clocks.json'


class RestorerTimeSeedTest(unittest.TestCase):
    def test_parallel_receipts_consume_all_capacity(self):
        rows = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(hours(rows), 8)
        self.assertEqual(remaining(rows), {'used': 8, 'remaining': 0})


if __name__ == '__main__':
    unittest.main()
