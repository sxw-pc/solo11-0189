import json
import unittest
from pathlib import Path
from bookconserve.supplier_offers import offers_for
from bookconserve.purchase_plan import choose

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'tissue-offers.json'

class ProcurementSeedTest(unittest.TestCase):
    def test_low_unit_price_ignores_delivery_and_minimum(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(len(offers_for(data['offers'], data['material'])), 2)
        self.assertEqual(choose(data['offers'], data['material']), 'V1')

if __name__ == '__main__':
    unittest.main()
