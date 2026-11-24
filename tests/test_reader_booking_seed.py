import json
import unittest
from pathlib import Path
from bookconserve.reader_requests import for_volume
from bookconserve.reading_slots import confirmations

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'reading-room.json'

class ReaderBookingSeedTest(unittest.TestCase):
    def test_same_volume_and_hour_confirmed_twice(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(len(for_volume(data['requests'], 'MS-41')), 2)
        self.assertEqual(confirmations(data['requests'], 'MS-41', data['open_hours']),
                         {'reader-A': True, 'reader-B': True})

if __name__ == '__main__':
    unittest.main()
