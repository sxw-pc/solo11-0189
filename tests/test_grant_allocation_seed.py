import json
import unittest
from pathlib import Path
from bookconserve.claim_capture import claimable
from bookconserve.grant_allocation import allocate

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'shared-grants.json'

class GrantAllocationSeedTest(unittest.TestCase):
    def test_shared_line_claimed_twice(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(len(claimable(data['expenses'], 'G12')), 2)
        self.assertEqual(len(claimable(data['expenses'], 'G13')), 2)
        self.assertEqual(allocate(data['expenses'], data['grants']),
                         {'G12': 1000, 'G13': 1000})

if __name__ == '__main__':
    unittest.main()
