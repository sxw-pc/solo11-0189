import json
import unittest
from pathlib import Path
from bookconserve.waste_capture import weight_for
from bookconserve.pickup_manifest import ready

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'workshop-discard.json'

class WasteSeedTest(unittest.TestCase):
    def test_below_capacity_mixed_container_is_approved(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(weight_for(data['entries'], 'D1'), 9)
        self.assertTrue(ready(data['entries'], 'D1', 10))

if __name__ == '__main__':
    unittest.main()
