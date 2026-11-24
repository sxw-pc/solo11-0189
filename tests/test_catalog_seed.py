import unittest
from pathlib import Path
from bookconserve.catalog_capture import read_page
from bookconserve.catalog_snapshot import build

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'


class CatalogSeedTest(unittest.TestCase):
    def test_first_page_looks_falsely_complete(self):
        self.assertEqual(read_page(ROOT / 'catalog-page-1.html'), ['V1'])
        self.assertEqual(build(ROOT / 'catalog-page-1.html'), {'complete': True, 'volumes': ['V1']})
        self.assertEqual(read_page(ROOT / 'catalog-page-2.html'), ['V2'])


if __name__ == '__main__':
    unittest.main()
