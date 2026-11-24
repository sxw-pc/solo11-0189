import unittest
from pathlib import Path

from bookconserve.catalog_capture import CaptureStore, parse_page, read_page
from bookconserve.catalog_snapshot import build

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'


def page_html(*, library='east', revision, page, sort_start, sort_end, entries=(),
              tombstones=(), next=None, digest=None):
    attrs = (f'data-library="{library}" data-revision="{revision}" data-page="{page}" '
             f'data-sort-start="{sort_start}" data-sort-end="{sort_end}"')
    if digest is not None:
        attrs += (f' data-digest-serial="{digest["serial"]}"'
                  f' data-digest-pages="{digest["pages"]}"'
                  f' data-digest-volumes="{digest["volumes"]}"'
                  f' data-digest-tombstones="{digest["tombstones"]}"')
    parts = [f'<html><body {attrs}>']
    for accession, volume, title, edition, sort in entries:
        parts.append(f'<div data-volume="{volume}" data-accession="{accession}"'
                     f' data-edition="{edition}" data-sort="{sort}">{title}</div>')
    for kind, accession, volume, title, sort, to_sort in tombstones:
        extra = f' data-to-sort="{to_sort}"' if to_sort else ''
        parts.append(f'<div data-tombstone="{kind}" data-accession="{accession}"'
                     f' data-volume="{volume}" data-sort="{sort}"{extra}>{title}</div>')
    if next is not None:
        parts.append(f'<a data-next="{next}">Next</a>')
    parts.append('</body></html>')
    return ''.join(parts)


def make_page(**kwargs):
    return parse_page(page_html(**kwargs))


def digest(revision, pages, volumes, tombstones=0, library='east', serial=None):
    return {'library': library, 'revision': revision,
            'serial': serial or f'{revision}-issued',
            'pages': pages, 'volumes': volumes, 'tombstones': tombstones}


C5_DIGEST = digest('C5', pages=2, volumes=2)
C6_DIGEST = digest('C6', pages=2, volumes=2, tombstones=1)
V0_TOMBSTONE = {'kind': 'withdrawn', 'accession': 'EAST-0000', 'volume': 'V0',
                'title': '古籍零', 'sort': 'A-02', 'to_sort': None}


def c5_page2_cache():
    """Pre-authorised supplement: the cached second page of the C5 cut (holds V0)."""
    return make_page(revision='C5', page=2, sort_start='A-02', sort_end='A-02',
                     entries=[('EAST-0000', 'V0', '古籍零', 'E1', 'A-02')])


def c6_export():
    """Pre-authorised supplement: C6 full-page export with consistency cursor."""
    page1 = make_page(revision='C6', page=1, sort_start='A-01', sort_end='A-01',
                      entries=[('EAST-0001', 'V1', '古籍甲', 'E1', 'A-01')],
                      next='catalog-page-2.html', digest=C6_DIGEST)
    page2 = make_page(revision='C6', page=2, sort_start='A-02', sort_end='A-02',
                      entries=[('EAST-0002', 'V2', '古籍乙', 'E1', 'A-02')],
                      digest=C6_DIGEST)
    return [page1, page2]


class CatalogSeedTest(unittest.TestCase):
    def mixed_store(self):
        """The fixed mixed crawl: C5 page 1 followed by the redesigned C6 page 2."""
        store = CaptureStore()
        run = store.crawl([read_page(ROOT / 'catalog-page-1.html'),
                           read_page(ROOT / 'catalog-page-2.html')])
        return store, run

    def route_a(self, store):
        """Supplement route one: C5 issuance summary + C5 page-2 cache -> C5 is V0+V1."""
        store.add_digest(C5_DIGEST)
        store.crawl([c5_page2_cache()])

    def route_b(self, store):
        """Supplement route two: C6 consistent full export + V0 tombstone -> C6 is V1+V2."""
        store.add_tombstone('east', 'C6', dict(V0_TOMBSTONE))
        store.crawl(c6_export())

    def test_fixture_pages_bind_cursors(self):
        first = read_page(ROOT / 'catalog-page-1.html')
        self.assertEqual(first['revision'], 'C5')
        self.assertEqual(first['cursor'], {'library': 'east', 'revision': 'C5', 'page': 1,
                                           'sort_start': 'A-01', 'sort_end': 'A-01'})
        self.assertEqual([e['volume'] for e in first['entries']], ['V1'])
        self.assertEqual(first['entries'][0]['accession'], 'EAST-0001')
        self.assertEqual(first['entries'][0]['edition'], 'E1')
        self.assertEqual(first['next'], 'catalog-page-2.html')
        second = read_page(ROOT / 'catalog-page-2.html')
        self.assertEqual(second['revision'], 'C6')
        self.assertEqual(second['cursor']['page'], 2)
        self.assertEqual([e['volume'] for e in second['entries']], ['V2'])

    def test_mixed_crawl_cannot_prove_any_frozen_revision(self):
        store, run = self.mixed_store()
        self.assertEqual(run['status'], 'unfinished')
        self.assertEqual([w['kind'] for w in run['witnesses']], ['chain-break'])
        c5 = build(store, 'east', 'C5')
        self.assertFalse(c5['complete'])
        self.assertEqual(c5['volumes'], [])
        self.assertIn({'kind': 'unclosed-next', 'page': 1, 'next': 'catalog-page-2.html'},
                      c5['gaps'])
        c6 = build(store, 'east', 'C6')
        self.assertFalse(c6['complete'])
        self.assertEqual(c6['volumes'], [])
        self.assertIn({'kind': 'missing-pages', 'pages': [1]}, c6['gaps'])
        current = build(store, 'east')
        self.assertFalse(current['complete'])
        self.assertEqual(current['volumes'], [])

    def test_route_a_restores_c5_cut(self):
        store, run = self.mixed_store()
        self.route_a(store)
        c5 = build(store, 'east', 'C5')
        self.assertTrue(c5['complete'])
        self.assertEqual({v['volume'] for v in c5['volumes']}, {'V0', 'V1'})
        self.assertEqual(c5['tombstones'], [])
        current = build(store, 'east')
        self.assertEqual(current['revision'], 'C5')
        self.assertEqual({v['volume'] for v in current['volumes']}, {'V0', 'V1'})
        # V2 waits for the next revision: C6 is still open, pending its page 1.
        c6 = build(store, 'east', 'C6')
        self.assertFalse(c6['complete'])
        self.assertIn({'kind': 'missing-pages', 'pages': [1]}, c6['gaps'])
        self.assertNotIn('V2', {v['volume'] for v in current['volumes']})
        self.assertEqual(run['status'], 'unfinished')

    def test_route_b_confirms_c6_cut(self):
        store, run = self.mixed_store()
        self.route_b(store)
        c6 = build(store, 'east', 'C6')
        self.assertTrue(c6['complete'])
        self.assertEqual([v['volume'] for v in c6['volumes']], ['V1', 'V2'])
        self.assertEqual([t['volume'] for t in c6['tombstones']], ['V0'])
        self.assertEqual(c6['tombstones'][0]['kind'], 'withdrawn')
        current = build(store, 'east')
        self.assertEqual(current['revision'], 'C6')
        self.assertEqual({v['volume'] for v in current['volumes']}, {'V1', 'V2'})
        # C5 is still open, pending its page 2.
        c5 = build(store, 'east', 'C5')
        self.assertFalse(c5['complete'])
        self.assertIn({'kind': 'unclosed-next', 'page': 1, 'next': 'catalog-page-2.html'},
                      c5['gaps'])
        self.assertEqual(run['status'], 'unfinished')

    def test_routes_do_not_cross_contaminate(self):
        store, run = self.mixed_store()
        self.route_a(store)
        self.route_b(store)
        c5 = build(store, 'east', 'C5')
        c6 = build(store, 'east', 'C6')
        self.assertTrue(c5['complete'])
        self.assertTrue(c6['complete'])
        # The C6 withdrawal tombstone must not rewrite the C5 originals.
        self.assertIn('V0', {v['volume'] for v in c5['volumes']})
        self.assertEqual(c5['tombstones'], [])
        self.assertEqual([t['volume'] for t in c6['tombstones']], ['V0'])
        self.assertNotIn('V0', {v['volume'] for v in c6['volumes']})
        # The original mixed attempt stays unfinished.
        self.assertEqual(store.runs[0]['status'], 'unfinished')
        self.assertTrue(any(w['kind'] == 'chain-break' for w in c5['witnesses']))
        current = build(store, 'east')
        self.assertEqual(current['revision'], 'C6')
        self.assertEqual(current['gaps'], [])

    def test_out_of_order_pages_still_close(self):
        store = CaptureStore()
        page1 = make_page(revision='C8', page=1, sort_start='A-01', sort_end='A-01',
                          entries=[('EAST-0001', 'V1', '古籍甲', 'E1', 'A-01')], next='p2')
        page2 = make_page(revision='C8', page=2, sort_start='A-02', sort_end='A-02',
                          entries=[('EAST-0002', 'V2', '古籍乙', 'E1', 'A-02')])
        store.add_page(page2)
        store.add_page(page1)
        store.add_digest(digest('C8', pages=2, volumes=2))
        snap = build(store, 'east', 'C8')
        self.assertTrue(snap['complete'])
        self.assertEqual([v['volume'] for v in snap['volumes']], ['V1', 'V2'])

    def test_sort_boundary_overlap_blocks_closure(self):
        store = CaptureStore()
        page1 = make_page(revision='C9', page=1, sort_start='A-01', sort_end='A-02',
                          entries=[('EAST-0001', 'V1', '古籍甲', 'E1', 'A-01'),
                                   ('EAST-0002', 'V2', '古籍乙', 'E1', 'A-02')], next='p2')
        page2 = make_page(revision='C9', page=2, sort_start='A-02', sort_end='A-03',
                          entries=[('EAST-0003', 'V3', '古籍丙', 'E1', 'A-03')])
        store.crawl([page1, page2])
        store.add_digest(digest('C9', pages=2, volumes=3))
        snap = build(store, 'east', 'C9')
        self.assertFalse(snap['complete'])
        self.assertEqual(snap['volumes'], [])
        self.assertTrue(any(c['kind'] == 'sort-boundary-overlap' for c in snap['conflicts']))

    def test_same_title_distinct_accessions_stay_two_records(self):
        store = CaptureStore()
        page1 = make_page(revision='C10', page=1, sort_start='A-01', sort_end='A-02',
                          entries=[('EAST-0001', 'V1', '古籍甲', 'E1', 'A-01'),
                                   ('EAST-0003', 'V3', '古籍甲', 'E1', 'A-02')])
        store.add_page(page1)
        store.add_digest(digest('C10', pages=1, volumes=2))
        snap = build(store, 'east', 'C10')
        self.assertTrue(snap['complete'])
        self.assertEqual(len(snap['volumes']), 2)
        self.assertEqual({v['accession'] for v in snap['volumes']},
                         {'EAST-0001', 'EAST-0003'})
        self.assertEqual([v['title'] for v in snap['volumes']], ['古籍甲', '古籍甲'])

    def test_same_accession_new_edition_supersedes_not_merges(self):
        store, _ = self.mixed_store()
        self.route_a(store)
        page1 = make_page(revision='C7', page=1, sort_start='A-01', sort_end='A-01',
                          entries=[('EAST-0001', 'V1', '古籍甲', 'E2', 'A-01')])
        store.add_page(page1)
        store.add_digest(digest('C7', pages=1, volumes=1))
        current = build(store, 'east')
        self.assertEqual(current['revision'], 'C7')
        self.assertEqual(len(current['volumes']), 1)
        self.assertEqual(current['volumes'][0]['edition'], 'E2')
        self.assertIn({'accession': 'EAST-0001', 'volume': 'V1', 'from_edition': 'E1',
                       'to_edition': 'E2', 'from_revision': 'C5', 'to_revision': 'C7'},
                      current['superseded'])

    def test_tombstones_withdraw_and_relocate(self):
        store = CaptureStore()
        page1 = make_page(revision='C11', page=1, sort_start='A-01', sort_end='A-01',
                          entries=[('EAST-0001', 'V1', '古籍甲', 'E1', 'A-01')],
                          tombstones=[('withdrawn', 'EAST-0000', 'V0', '古籍零', 'A-02', None),
                                      ('relocated', 'EAST-0005', 'V5', '古籍戊', 'A-00', 'A-03')],
                          next='p2')
        page2 = make_page(revision='C11', page=2, sort_start='A-02', sort_end='A-03',
                          entries=[('EAST-0003', 'V3', '古籍丙', 'E1', 'A-02'),
                                   ('EAST-0005', 'V5', '古籍戊', 'E1', 'A-03')])
        store.crawl([page1, page2])
        store.add_digest(digest('C11', pages=2, volumes=3, tombstones=2))
        snap = build(store, 'east', 'C11')
        self.assertTrue(snap['complete'])
        self.assertEqual([v['volume'] for v in snap['volumes']], ['V1', 'V3', 'V5'])
        kinds = {t['volume']: t['kind'] for t in snap['tombstones']}
        self.assertEqual(kinds, {'V0': 'withdrawn', 'V5': 'relocated'})

    def test_duplicate_accession_across_pages_is_conflict(self):
        store = CaptureStore()
        page1 = make_page(revision='C12', page=1, sort_start='A-01', sort_end='A-01',
                          entries=[('EAST-0001', 'V1', '古籍甲', 'E1', 'A-01')], next='p2')
        page2 = make_page(revision='C12', page=2, sort_start='A-02', sort_end='A-02',
                          entries=[('EAST-0001', 'V1', '古籍甲', 'E1', 'A-02')])
        store.crawl([page1, page2])
        store.add_digest(digest('C12', pages=2, volumes=1))
        snap = build(store, 'east', 'C12')
        self.assertFalse(snap['complete'])
        self.assertEqual(snap['volumes'], [])
        self.assertTrue(any(c['kind'] == 'duplicate-accession' for c in snap['conflicts']))

    def test_mixed_digest_cut_is_conflict(self):
        store = CaptureStore()
        page1 = make_page(revision='C13', page=1, sort_start='A-01', sort_end='A-01',
                          entries=[('EAST-0001', 'V1', '古籍甲', 'E1', 'A-01')], next='p2',
                          digest=digest('C13', 2, 2, serial='C13-a'))
        page2 = make_page(revision='C13', page=2, sort_start='A-02', sort_end='A-02',
                          entries=[('EAST-0002', 'V2', '古籍乙', 'E1', 'A-02')],
                          digest=digest('C13', 2, 2, serial='C13-b'))
        store.crawl([page1, page2])
        snap = build(store, 'east', 'C13')
        self.assertFalse(snap['complete'])
        self.assertTrue(any(c['kind'] == 'digest-mismatch' for c in snap['conflicts']))

    def test_replay_idempotent_and_changed_page_conflicts(self):
        store = CaptureStore()
        page = read_page(ROOT / 'catalog-page-1.html')
        self.assertEqual(store.add_page(page)['status'], 'added')
        self.assertEqual(store.add_page(page)['status'], 'replayed')
        self.assertEqual(len(store.batch('east', 'C5')['pages']), 1)
        changed = make_page(revision='C5', page=1, sort_start='A-01', sort_end='A-01',
                            entries=[('EAST-0001', 'V1', '古籍甲（改）', 'E1', 'A-01')],
                            next='other.html')
        self.assertEqual(store.add_page(changed)['status'], 'conflict')
        snap = build(store, 'east', 'C5')
        self.assertFalse(snap['complete'])
        self.assertTrue(any(c['kind'] == 'page-content-mismatch' for c in snap['conflicts']))

    def test_late_pages_recompute_only_their_own_batch(self):
        store, _ = self.mixed_store()
        self.route_a(store)
        before = build(store, 'east', 'C5')
        self.route_b(store)
        self.assertEqual(build(store, 'east', 'C5'), before)
        west = make_page(library='west', revision='C1', page=1,
                         sort_start='W-01', sort_end='W-01',
                         entries=[('WEST-0001', 'V9', '西馆卷九', 'E1', 'W-01')])
        store.add_page(west)
        store.add_digest(digest('C1', 1, 1, library='west'))
        self.assertEqual(build(store, 'east', 'C5'), before)
        self.assertTrue(build(store, 'west', 'C1')['complete'])
        self.assertEqual(build(store, 'east', 'C5'), before)


if __name__ == '__main__':
    unittest.main()
