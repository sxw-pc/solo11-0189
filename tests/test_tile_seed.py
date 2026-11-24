import json
import unittest
from pathlib import Path
from bookconserve.tile_capture import uploads
from bookconserve.page_archive import assess

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'page-tiles.json'


class TileSeedTest(unittest.TestCase):
    def test_upload_count_overstates_physical_page_coverage(self):
        rows = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(uploads(rows), 3)
        self.assertEqual(assess(rows), {'uploads': 3, 'status': '完整'})
        self.assertEqual(len({row['region'] for row in rows}), 2)


if __name__ == '__main__':
    unittest.main()
