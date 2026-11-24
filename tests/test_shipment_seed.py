import json
import unittest
from pathlib import Path
from bookconserve.route_capture import candidate_routes
from bookconserve.shipment_quote import choose

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'book-shipment.json'

class ShipmentSeedTest(unittest.TestCase):
    def test_cheapest_bare_route_is_selected(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(len(candidate_routes(data['routes'], data['origin'], data['destination'])), 3)
        self.assertEqual(choose(data['routes'], data['origin'], data['destination']), 'bare-direct')

if __name__ == '__main__':
    unittest.main()
