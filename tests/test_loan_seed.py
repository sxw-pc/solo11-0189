import json
import unittest
from pathlib import Path
from bookconserve.loan_capture import latest
from bookconserve.loan_reconcile import whereabouts

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'loan-split.json'


class LoanSeedTest(unittest.TestCase):
    def test_whole_package_return_masks_item_split(self):
        record = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(latest(record['events'])['id'], 'return-crate')
        self.assertEqual(whereabouts(record['events'], record['manifest']), {'V1': 'museum', 'F12': 'museum'})
        self.assertEqual(next(e for e in record['events'] if e['id'] == 'folio-lab')['location'], 'lab')


if __name__ == '__main__':
    unittest.main()
