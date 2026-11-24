import json
import tempfile
import unittest
from pathlib import Path

from bookconserve.loan_capture import (
    add_loan_events,
    build_boxes,
    deduplicate,
    latest,
    ledger_events,
    load_loan_record,
    open_ledger,
    order_events,
    prepare_events,
    signed_event,
    validate_event,
)
from bookconserve.loan_reconcile import (
    CONFIRMED,
    PENDING,
    reconcile,
    route_a_disposition,
    route_b_disposition,
    whereabouts,
)

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'loan-split.json'


def load():
    return load_loan_record(FIXTURE)


class SeedSplitTest(unittest.TestCase):
    """原始缺陷：整箱返馆扫描不能证明每件都已返还。"""

    def setUp(self):
        record = load()
        self.events = record['events']
        self.manifest = record['manifest']

    def test_latest_is_still_the_crate_receipt(self):
        # 采录层 latest 仍按 received_at 选最后一张箱级回执（保留旧接口语义）
        self.assertEqual(latest(self.events)['id'], 'return-crate')

    def test_box_receipt_does_not_decide_item_whereabouts(self):
        result = reconcile(self.events, self.manifest)
        custody = result['custody']
        # V1 从未拆出，可与开箱清单合读确认随箱返馆
        self.assertEqual(custody['V1']['status'], CONFIRMED)
        self.assertEqual(custody['V1']['location'], 'museum')
        # F12 只有单方送检扫码：待核，绝不被整箱返馆覆盖成 museum
        self.assertEqual(custody['F12']['status'], PENDING)
        self.assertEqual(custody['F12']['location'], 'lab')
        self.assertIsNone(custody['F12']['custodian'])

    def test_whereabouts_wrapper_no_longer_copies_box_location(self):
        self.assertEqual(whereabouts(self.events, self.manifest),
                         {'V1': 'museum', 'F12': 'lab'})

    def test_single_party_scan_is_unclosed_pending(self):
        result = reconcile(self.events, self.manifest)
        unclosed = result['unclosed_handoffs']
        self.assertEqual([u['event_id'] for u in unclosed], ['folio-lab'])
        self.assertEqual(unclosed[0]['kind'], 'single_party_scan')
        self.assertIn('待核', unclosed[0]['reason'])

    def test_return_crate_excluded_for_removed_folio(self):
        result = reconcile(self.events, self.manifest)
        excluded_ids = [e['event_id'] for e in result['excluded_receipts']]
        self.assertIn('return-crate', excluded_ids)
        note = next(e for e in result['excluded_receipts'] if e['event_id'] == 'return-crate')
        self.assertIn('箱级回执', note['reason'])
        self.assertIn('F12', note['reason'])

    def test_base_shortage_list_flags_only_unconfirmed_folio(self):
        result = reconcile(self.events, self.manifest)
        self.assertEqual([s['item'] for s in result['return_shortages']], ['F12'])
        # 基础证据不足，两路单据都不应凭空生成
        self.assertEqual(result['documents'], [])
        self.assertEqual(result['corrections'], [])

    def test_folio_keeps_old_whereabouts_versions(self):
        result = reconcile(self.events, self.manifest)
        versions = result['custody']['F12']['versions']
        # 送检扫码版本在前，迟到整箱回执只留痕且不覆盖
        self.assertEqual(versions[0]['event_id'], 'folio-lab')
        self.assertEqual(versions[0]['status'], PENDING)
        self.assertEqual(versions[-1]['event_id'], 'return-crate')
        self.assertEqual(versions[-1]['status'], PENDING)
        self.assertIn('不覆盖', versions[-1]['note'])


class BoxItemRelationTest(unittest.TestCase):
    """loan_capture 的箱/件关系与逐件采录规则。"""

    def test_signed_event_requires_two_distinct_parties(self):
        self.assertTrue(signed_event(
            {'action': 'item_handoff', 'from_party': 'gallery', 'to_party': 'lab'}))
        self.assertFalse(signed_event(
            {'action': 'item_handoff', 'from_party': 'gallery', 'to_party': 'gallery'}))
        self.assertFalse(signed_event({'action': 'unpack', 'party': 'gallery'}))

    def test_validate_event_rejects_unknown_and_incomplete(self):
        with self.assertRaises(ValueError):
            validate_event({'id': 'x', 'action': 'teleport', 'at': 1, 'received_at': 1})
        with self.assertRaises(ValueError):
            validate_event({'id': 'x', 'action': 'item_handoff', 'at': 1,
                            'received_at': 1, 'item': 'F12',
                            'from_party': 'g', 'to_party': 'g'})
        with self.assertRaises(ValueError):
            validate_event({'id': 'x', 'action': 'seal', 'at': 1, 'received_at': 1,
                            'package': 'C1'})

    def test_dedup_prefers_dual_signed_then_earliest(self):
        single = {'id': 's1', 'action': 'item_handoff', 'item': 'F12',
                  'from_party': 'gallery', 'to_party': None,
                  'location': 'lab', 'at': 14, 'received_at': 17}
        dual = {'id': 's2', 'action': 'item_handoff', 'item': 'F12',
                'from_party': 'gallery', 'to_party': 'lab',
                'location': 'lab', 'at': 15, 'received_at': 21}
        winners, dropped = deduplicate([single, dual])
        self.assertEqual([w['id'] for w in winners], ['s2'])
        self.assertEqual(dropped, ['s1'])

    def test_ordering_uses_at_before_received_at(self):
        late_arrival = {'id': 'late', 'action': 'inventory', 'item': 'F12',
                        'location': 'lab', 'party': 'lab', 'at': 9, 'received_at': 30}
        early = {'id': 'early', 'action': 'inventory', 'item': 'V1',
                 'location': 'museum', 'party': 'museum', 'at': 10, 'received_at': 10}
        self.assertEqual([e['id'] for e in order_events([late_arrival, early])],
                         ['late', 'early'])

    def test_build_boxes_tracks_removal_and_seal_restore(self):
        record = load()
        boxes = build_boxes(record['events'] + record['route_b_events'],
                            record['manifest'])
        b = boxes['C1']
        # 封签未开证明恢复后，F12 不再处于拆出集合
        self.assertTrue(b['seal_intact'])
        self.assertNotIn('F12', b['removed'])
        boxes_a = build_boxes(record['events'] + record['route_a_events'],
                              record['manifest'])
        self.assertFalse(boxes_a['C1']['seal_intact'])
        self.assertIn('F12', boxes_a['C1']['removed'])


class DualSignatureCustodyTest(unittest.TestCase):
    """双签、保管责任有效性与同物两处冲突。"""

    def _base_seal(self):
        return [
            {'id': 'seal', 'action': 'seal', 'package': 'C1', 'seal_id': 'S1',
             'location': 'museum', 'at': 1, 'received_at': 1, 'party': 'museum',
             'items': ['V1', 'F12']},
            {'id': 'out', 'action': 'receive', 'package': 'C1',
             'location': 'gallery', 'at': 2, 'received_at': 2,
             'party': 'carrier', 'to_party': 'gallery'},
        ]

    def test_dual_signed_handoff_closes_pending_and_transfers_custody(self):
        events = self._base_seal() + [
            {'id': 'open', 'action': 'open', 'package': 'C1',
             'location': 'gallery', 'at': 2, 'received_at': 2,
             'party': 'gallery', 'seal_id': 'S1', 'items': ['V1', 'F12']},
            {'id': 'scan', 'action': 'unpack', 'package': 'C1', 'item': 'F12',
             'location': 'lab', 'at': 3, 'received_at': 9, 'party': 'gallery'},
            {'id': 'hand', 'action': 'item_handoff', 'package': 'C1', 'item': 'F12',
             'from_party': 'gallery', 'to_party': 'lab', 'location': 'lab',
             'at': 4, 'received_at': 10},
            {'id': 'back', 'action': 'return', 'package': 'C1',
             'location': 'museum', 'at': 5, 'received_at': 5,
             'party': 'carrier', 'to_party': 'museum'},
        ]
        result = reconcile(events, ['V1', 'F12'])
        f12 = result['custody']['F12']
        self.assertEqual(f12['status'], CONFIRMED)
        self.assertEqual(f12['custodian'], 'lab')
        self.assertEqual(f12['location'], 'lab')
        self.assertEqual(result['unclosed_handoffs'], [])
        # 已拆出件不被整箱返馆覆盖，箱级回执出现在排除说明里
        excluded = [e['event_id'] for e in result['excluded_receipts']]
        self.assertIn('back', excluded)
        # 未拆出的 V1 仍随箱确认返馆
        self.assertEqual(result['custody']['V1']['location'], 'museum')

    def test_handoff_from_wrong_custodian_is_refused(self):
        events = self._base_seal() + [
            {'id': 'bad-hand', 'action': 'item_handoff', 'package': 'C1', 'item': 'F12',
             'from_party': 'stranger', 'to_party': 'lab', 'location': 'lab',
             'at': 4, 'received_at': 10},
        ]
        result = reconcile(events, ['V1', 'F12'])
        kinds = [u['kind'] for u in result['unclosed_handoffs']]
        self.assertIn('custody_mismatch', kinds)
        # 保管责任没有转移
        self.assertNotEqual(result['custody']['F12']['custodian'], 'lab')

    def test_same_item_two_places_same_time_excludes_late_claim(self):
        events = self._base_seal() + [
            {'id': 'inv-early', 'action': 'inventory', 'item': 'F12',
             'location': 'lab', 'party': 'lab', 'at': 20, 'received_at': 21},
            {'id': 'inv-late', 'action': 'inventory', 'item': 'F12',
             'location': 'museum', 'party': 'museum', 'at': 20, 'received_at': 29},
        ]
        result = reconcile(events, ['V1', 'F12'])
        self.assertEqual(result['custody']['F12']['location'], 'lab')
        excluded = [e['event_id'] for e in result['excluded_receipts']]
        self.assertIn('inv-late', excluded)
        # 旧去向版本保留
        self.assertTrue(any(v['event_id'] == 'inv-early'
                            for v in result['custody']['F12']['versions']))

    def test_late_receipt_only_recomputes_touched_item(self):
        events = self._base_seal() + [
            {'id': 'inv-f12', 'action': 'inventory', 'item': 'F12',
             'location': 'lab', 'party': 'lab', 'at': 20, 'received_at': 21},
            # 迟到的 V1 盘点，只应触及 V1 的版本链
            {'id': 'inv-v1-late', 'action': 'inventory', 'item': 'V1',
             'location': 'gallery', 'party': 'gallery', 'at': 5, 'received_at': 40},
        ]
        result = reconcile(events, ['V1', 'F12'])
        v1_ids = [v['event_id'] for v in result['custody']['V1']['versions']]
        f12_ids = [v['event_id'] for v in result['custody']['F12']['versions']]
        self.assertIn('inv-v1-late', v1_ids)
        self.assertNotIn('inv-v1-late', f12_ids)


class RouteADispositionTest(unittest.TestCase):
    """处置一：拆件属实，双签交接 + 在库盘点 -> 返馆缺件催回单。"""

    def setUp(self):
        record = load()
        self.result = route_a_disposition(
            record['events'], record['route_a_events'], record['manifest'])

    def test_v1_returned_f12_remains_in_lab(self):
        self.assertEqual(self.result['custody']['V1']['location'], 'museum')
        f12 = self.result['custody']['F12']
        self.assertEqual(f12['status'], CONFIRMED)
        self.assertEqual(f12['location'], 'lab')
        self.assertEqual(f12['custodian'], 'lab')

    def test_recall_document_and_shortage(self):
        self.assertEqual([s['item'] for s in self.result['return_shortages']], ['F12'])
        docs = self.result['documents']
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['type'], '返馆缺件催回单')
        self.assertEqual(docs[0]['missing'], ['F12'])
        self.assertEqual(docs[0]['confirmed_returned'], ['V1'])

    def test_correction_version_issued_against_wrong_all_returned(self):
        corrections = self.result['corrections']
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]['supersedes'], 'inference:whole-crate-returned')
        # 更正以新版本追加，F12 的历史版本没有被抹掉
        f12_events = [v['event_id'] for v in self.result['custody']['F12']['versions']]
        self.assertIn('a-correction', f12_events)
        self.assertIn('folio-lab', f12_events)

    def test_conflicting_box_receipt_explained(self):
        excluded = {e['event_id']: e['reason']
                    for e in self.result['excluded_receipts']}
        self.assertIn('return-crate', excluded)
        self.assertIn('F12', excluded['return-crate'])
        # 单方扫码被双签交接吸收闭合，不再挂未结
        self.assertEqual(self.result['unclosed_handoffs'], [])

    def test_route_a_must_not_use_seal_intact_proof(self):
        record = load()
        leak = record['route_b_events'][1]  # b-seal-intact
        with self.assertRaises(ValueError):
            route_a_disposition(record['events'],
                                record['route_a_events'] + [leak],
                                record['manifest'])


class RouteBDispositionTest(unittest.TestCase):
    """处置二：扫码误归，封签未开 + 逐件点验 + 纠错 -> 闭合返馆单。"""

    def setUp(self):
        record = load()
        self.result = route_b_disposition(
            record['events'], record['route_b_events'], record['manifest'])

    def test_both_items_confirmed_back_at_museum(self):
        for item in ('V1', 'F12'):
            view = self.result['custody'][item]
            self.assertEqual(view['status'], CONFIRMED, item)
            self.assertEqual(view['location'], 'museum', item)

    def test_closed_return_document_no_shortage(self):
        self.assertEqual(self.result['return_shortages'], [])
        docs = self.result['documents']
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['type'], '闭合返馆单')
        self.assertEqual(sorted(docs[0]['all_returned']), ['F12', 'V1'])

    def test_misdirected_scan_explained_as_misattribution(self):
        excluded = {e['event_id']: e['reason']
                    for e in self.result['excluded_receipts']}
        self.assertIn('folio-lab', excluded)
        self.assertIn('误归', excluded['folio-lab'])

    def test_correction_version_still_issued(self):
        self.assertEqual([c['event_id'] for c in self.result['corrections']],
                         ['b-correction'])

    def test_route_b_must_not_use_dual_signed_unpack_handoff(self):
        record = load()
        leak = next(e for e in record['route_a_events']
                    if e['action'] == 'item_handoff')
        with self.assertRaises(ValueError):
            route_b_disposition(record['events'],
                                record['route_b_events'] + [leak],
                                record['manifest'])

    def test_routes_are_independent_runs(self):
        # 两路结论互相隔离，不能在同一处置里同时成立
        record = load()
        with self.assertRaises(ValueError):
            route_b_disposition(
                record['events'],
                record['route_b_events'] + record['route_a_events'],
                record['manifest'])


class SealConflictTest(unittest.TestCase):
    """封签证据冲突纪律。"""

    def test_tampered_seal_conflicts_with_intact_proof(self):
        events = [
            {'id': 'seal-bad', 'action': 'seal', 'package': 'C1', 'seal_id': 'S9',
             'location': 'museum', 'at': 1, 'received_at': 1, 'party': 'museum',
             'tamper': True, 'items': ['V1']},
            {'id': 'proof', 'action': 'seal_proof', 'package': 'C1', 'seal_id': 'S9',
             'location': 'museum', 'at': 2, 'received_at': 3, 'intact': True},
        ]
        result = reconcile(events, ['V1'])
        excluded = [e['event_id'] for e in result['excluded_receipts']]
        self.assertIn('proof', excluded)

    def test_route_b_failure_without_seal_proof(self):
        record = load()
        stripped = [e for e in record['route_b_events']
                    if e['action'] != 'seal_proof']
        with self.assertRaises(ValueError):
            route_b_disposition(record['events'], stripped, record['manifest'])

    def test_route_a_failure_without_dual_signed_handoff(self):
        record = load()
        stripped = [e for e in record['route_a_events']
                    if e['action'] != 'item_handoff']
        with self.assertRaises(ValueError):
            route_a_disposition(record['events'], stripped, record['manifest'])


class LedgerAndLegacyTest(unittest.TestCase):
    """SQLite 事务账本与旧版无拆件借展夹具可读性。"""

    def test_ledger_commit_replay_and_collision_rollback(self):
        record = load()
        with tempfile.TemporaryDirectory() as tmp:
            db = open_ledger(Path(tmp) / 'loan.sqlite')
            first = add_loan_events(db, record['events'])
            self.assertEqual(first['inserted'], len(record['events']))
            replay = add_loan_events(db, record['events'])
            self.assertEqual(replay['replayed'], len(record['events']))
            self.assertEqual(replay['inserted'], 0)
            stored = ledger_events(db)
            self.assertEqual([e['id'] for e in stored],
                             [e['id'] for e in order_events(record['events'])])
            tampered = dict(record['events'][0], location='elsewhere')
            with self.assertRaises(ValueError):
                add_loan_events(db, [tampered])
            # 回滚后账本仍是原始事件
            self.assertEqual(len(ledger_events(db)), len(record['events']))
            db.close()

    def test_legacy_manifest_without_split_still_reads(self):
        # 旧夹具形态：只有箱级 outbound/receive/return，无封签、无拆件
        legacy = [
            {'id': 'out', 'action': 'handoff', 'package': 'X1',
             'location': 'carrier', 'at': 1, 'received_at': 1,
             'party': 'museum', 'to_party': 'carrier'},
            {'id': 'recv', 'action': 'receive', 'package': 'X1',
             'location': 'gallery', 'at': 2, 'received_at': 2,
             'party': 'carrier', 'to_party': 'gallery'},
            {'id': 'back', 'action': 'return', 'package': 'X1',
             'location': 'museum', 'at': 3, 'received_at': 3,
             'party': 'carrier', 'to_party': 'museum'},
        ]
        prepared, dropped = prepare_events(legacy)
        self.assertEqual(dropped, [])
        result = reconcile(prepared, ['K1'])
        self.assertEqual(result['custody']['K1']['location'], 'museum')
        self.assertEqual(result['custody']['K1']['status'], CONFIRMED)
        self.assertEqual(result['return_shortages'], [])
        self.assertEqual(whereabouts(prepared, ['K1']), {'K1': 'museum'})

    def test_reconcile_does_not_mutate_input_events(self):
        record = load()
        snapshot = json.loads(json.dumps(record['events']))
        reconcile(record['events'], record['manifest'])
        self.assertEqual(record['events'], snapshot)

    def test_fixture_file_shape(self):
        record = load()
        self.assertEqual(record['manifest'], ['V1', 'F12'])
        self.assertTrue(record['route_a_events'])
        self.assertTrue(record['route_b_events'])
        all_ids = [e['id'] for e in record['events']
                   + record['route_a_events'] + record['route_b_events']]
        self.assertEqual(len(all_ids), len(set(all_ids)))


if __name__ == '__main__':
    unittest.main()
