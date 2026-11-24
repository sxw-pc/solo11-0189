import json
import unittest
from pathlib import Path

from bookconserve.waste_capture import (
    WasteLedger, LedgerError, weight_for,
)
from bookconserve.pickup_manifest import (
    PickupLedger, ManifestError, evaluate_container, ready,
    categories_compatible, MIXING_COMPATIBILITY,
    DELIVERABLE, BLOCKED, DELIVERED, LEGACY_PENDING, REVISED,
)

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'workshop-discard.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def stream(data, branch):
    return list(data['event_streams']['prologue']) + list(data['event_streams'][branch])


class LegacySeedTest(unittest.TestCase):
    """原有修复夹具：重量聚合仍可复现，但容量不再单独放行混装。"""

    def setUp(self):
        self.data = load_fixture()

    def test_weight_aggregation_preserved(self):
        self.assertEqual(weight_for(self.data['entries'], 'D1'), 9)

    def test_capacity_alone_does_not_approve_mixed_uncertified_container(self):
        # 旧接口在无事件账本时维持纯容量布尔语义（仅聚合，不构成核证）。
        self.assertTrue(ready(self.data['entries'], 'D1', 10))
        # 修复后：序言事件流显示 D1 混装且 w2 分类未核证，必须暂缓交付。
        ledger = PickupLedger(self.data['event_streams']['prologue'])
        self.assertFalse(ready(self.data['entries'], 'D1', 10, ledger=ledger))
        status = ledger.delivery_status('D1', 10)
        self.assertEqual(status['status'], BLOCKED)
        self.assertTrue(any('兼容表' in r or '分类' in r for r in status['reasons']))
        self.assertEqual(
            [b['batch'] for b in ledger.waste.quarantine_batches()], ['w2'])


class CompatibilityTableTest(unittest.TestCase):
    def test_solvent_pads_never_mix_with_wet_cellulose(self):
        self.assertFalse(categories_compatible(['wet-cellulose', 'solvent-pads']))
        self.assertTrue(categories_compatible(['wet-cellulose', 'water-based-pads']))
        self.assertTrue(categories_compatible(['solvent-pads']))
        self.assertTrue(categories_compatible(['wet-cellulose']))
        # 兼容表必须显式不含任何含溶剂垫的混装对。
        self.assertFalse(any('solvent-pads' in pair for pair in MIXING_COMPATIBILITY))

    def test_capacity_cannot_replace_classification(self):
        data = load_fixture()
        ledger = PickupLedger(data['event_streams']['prologue'])
        check = evaluate_container(ledger.waste, 'D1', capacity_kg=100)  # 放大容量
        self.assertFalse(check['ok'])
        self.assertTrue(any('兼容表' in r or '分类' in r for r in check['reasons']))


class WaterBasedCertificationTest(unittest.TestCase):
    """核证路径一：双证证明 w2 为无溶剂水基垫，D1 9kg 同桶签发。"""

    def setUp(self):
        self.data = load_fixture()
        self.ledger = PickupLedger(stream(self.data, 'water_based_certified'))

    def test_w2_reclassified_with_evidence_chain(self):
        w2 = self.ledger.waste.batches['w2']
        self.assertEqual(w2['classification'], 'water-based')
        self.assertEqual(w2['category'], 'water-based-pads')
        bases = {e['basis'] for e in w2['evidence']}
        self.assertEqual(bases, {'supplier-cert', 'inhouse-review'})
        # 原始申报的含溶剂分类争议必须保留在批次状态里。
        self.assertEqual(w2['declared_category'], 'solvent-pads')

    def test_d1_delivered_as_single_nine_kg_manifest(self):
        status = self.ledger.delivery_status('D1', 10)
        self.assertEqual(status['status'], DELIVERABLE)
        self.assertEqual(status['contents']['net_kg'], 9)
        self.assertEqual(
            {item['batch'] for item in status['contents']['items']}, {'w1', 'w2'})
        manifest = self.ledger.manifests['MF-A-01']
        self.assertEqual(manifest['status'], DELIVERED)
        self.assertEqual(manifest['driver'], '清运司机-钱进')
        self.assertIsNotNone(manifest['signed_at'])
        # 只有一个容器进入交接链，没有制造 D2 实物。
        self.assertIsNone(self.ledger.waste.container_contents('D2'))
        # 原拟签清单未被覆盖，仍记录其暂缓原因与原签发时间/责任人。
        orig = self.ledger.manifests['MF-ORIG']
        self.assertEqual(orig['status'], BLOCKED)
        self.assertEqual(orig['issued_at'], '2026-11-20T09:00:00')
        self.assertEqual(orig['issuer'], '馆员-赵宁')
        self.assertIsNone(orig['signed_at'])


class SolventSplitBeforePickupTest(unittest.TestCase):
    """核证路径二：含溶剂结论在清运方接收前到达，现场拆封分装为两桶双签。"""

    def setUp(self):
        self.data = load_fixture()
        self.ledger = PickupLedger(stream(self.data, 'solvent_confirmed_split_before_pickup'))

    def test_solvent_classification_blocks_original_d1(self):
        w2 = self.ledger.waste.batches['w2']
        self.assertEqual(w2['classification'], 'solvent')
        self.assertEqual(w2['category'], 'solvent-pads')

    def test_split_conserves_weight_and_locations(self):
        d1 = self.ledger.waste.container_contents('D1')
        d2 = self.ledger.waste.container_contents('D2')
        self.assertEqual([i['batch'] for i in d1['items']], ['w1'])
        self.assertEqual(d1['net_kg'], 6)
        self.assertEqual([i['batch'] for i in d2['items']], ['w2'])
        self.assertEqual(d2['net_kg'], 3)
        # 守恒：6 + 3 仍等于来源工单登记的 9kg，且每批次只在一个容器。
        self.assertEqual(d1['net_kg'] + d2['net_kg'], 9)
        self.assertEqual(self.ledger.waste.batches['w1']['location'], 'D1')
        self.assertEqual(self.ledger.waste.batches['w2']['location'], 'D2')
        # 拆封后源桶按皮重复称，毛重不残留封签前读数。
        self.assertEqual(
            self.ledger.waste.containers['D1']['gross_kg'],
            self.ledger.waste.containers['D1']['tare_kg'] + 6)

    def test_two_sealed_manifests_signed_independently(self):
        for manifest_id, container in (('MF-B-D1', 'D1'), ('MF-B-D2', 'D2')):
            manifest = self.ledger.manifests[manifest_id]
            self.assertEqual(manifest['status'], DELIVERED)
            self.assertEqual(manifest['container'], container)
            self.assertIsNotNone(manifest['signed_at'])
        # 封签号不可复用：D1 经历 S-D1-001 拆封与 S-D1-002 重封。
        seals = [h['seal_id'] for h in
                 self.ledger.waste.containers['D1']['history'] if 'seal_id' in h]
        self.assertEqual(seals, ['S-D1-001', 'S-D1-001', 'S-D1-002'])
        self.assertEqual(self.ledger.manifests['MF-B-D2']['seal_id'], 'S-D2-001')

    def test_split_requires_unseal_first(self):
        events = stream(self.data, 'solvent_confirmed_split_before_pickup')
        # 删除拆封事件后，分装必须被拒绝。
        stripped = [e for e in events if e['id'] != 'b-unseal']
        with self.assertRaises(LedgerError):
            PickupLedger(stripped)

    def test_split_cannot_create_mass(self):
        events = json.loads(json.dumps(stream(
            self.data, 'solvent_confirmed_split_before_pickup')))
        for event in events:
            if event['id'] == 'b-split-w2':
                event['data']['kg'] = 4          # 桶内只有 3kg
                event['data']['gross_kg'] = 4.8
        with self.assertRaises(LedgerError):
            PickupLedger(events)


class DriverSignedRecallTest(unittest.TestCase):
    """核证路径三：司机已签 D1 后检测证实含溶剂——冻结、追回、复称更正。"""

    def setUp(self):
        self.data = load_fixture()
        self.ledger = PickupLedger(stream(
            self.data, 'solvent_confirmed_after_driver_signed'))

    def test_legacy_signature_is_recorded_but_not_accepted_as_delivery(self):
        # 在 freeze 事件之前重放到遗留签收：旧规则既成签收只登记为待核，不产生交付效力。
        pre_freeze = [e for e in stream(
            self.data, 'solvent_confirmed_after_driver_signed')
            if e['id'] in {x['id'] for x in self.data['event_streams']['prologue']}
            or e['id'] == 'c-legacy-sign']
        interim = PickupLedger(pre_freeze)
        self.assertEqual(interim.manifests['MF-ORIG']['status'], LEGACY_PENDING)
        # 新规则永远拒绝把未核证清单当作可签收交付。
        with self.assertRaises(ManifestError):
            PickupLedger([
                e for e in stream(self.data, 'water_based_certified')
                if e['id'] != 'wb-supplier' and e['id'] != 'wb-inhouse'
            ] + [{
                'subsystem': 'workshop-waste', 'id': 'forged-sign', 'subject': 'D1',
                'action': 'sign', 'at': '2026-11-20T12:00:00', 'version': 1,
                'data': {'manifest_id': 'MF-ORIG', 'container': 'D1',
                         'museum_party': '馆员-赵宁', 'carrier_party': '清运司机-钱进',
                         'driver': '清运司机-钱进'},
            }])
        orig = self.ledger.manifests['MF-ORIG']
        # 原签发时间与责任司机原样保留。
        self.assertEqual(orig['issued_at'], '2026-11-20T09:00:00')
        self.assertEqual(orig['signed_at'], '2026-11-20T09:20:00')
        self.assertEqual(orig['driver'], '清运司机-钱进')
        self.assertEqual(orig['issuer'], '馆员-赵宁')

    def test_original_manifest_frozen_then_superseded_not_overwritten(self):
        orig = self.ledger.manifests['MF-ORIG']
        self.assertEqual(orig['status'], REVISED)
        self.assertEqual(orig['superseded_by'], ['MF-C-D1', 'MF-C-D2'])
        history_actions = [h['action'] for h in orig['history']]
        self.assertIn('freeze', history_actions)
        self.assertIn('recall', history_actions)
        self.assertIn('correct_issue', history_actions)

    def test_recall_must_match_physical_seal_held_by_driver(self):
        events = json.loads(json.dumps(stream(
            self.data, 'solvent_confirmed_after_driver_signed')))
        for event in events:
            if event['id'] == 'c-recall':
                event['data']['seal_id'] = 'S-FAKE-999'   # 凭补录制造封签
        with self.assertRaises(ManifestError):
            PickupLedger(events)

    def test_freeze_required_before_recall(self):
        events = stream(self.data, 'solvent_confirmed_after_driver_signed')
        stripped = [e for e in events if e['id'] != 'c-freeze']
        with self.assertRaises(ManifestError):
            PickupLedger(stripped)

    def test_correction_manifests_chain_back_to_original(self):
        chain = self.ledger.manifest_revision_chain('MF-ORIG')
        self.assertEqual(len(chain), 1)
        root = chain[0]
        self.assertEqual(root['manifest_id'], 'MF-ORIG')
        correction_ids = {c['manifest_id'] for c in root['corrections']}
        self.assertEqual(correction_ids, {'MF-C-D1', 'MF-C-D2'})
        for correction in root['corrections']:
            self.assertEqual(correction['linked_from'], 'MF-ORIG')
            self.assertEqual(correction['status'], DELIVERED)
            self.assertEqual(correction['driver'], '清运司机-钱进')
            self.assertGreater(correction['issued_at'], root['issued_at'])

    def test_physical_split_still_conserves_after_recall(self):
        d1 = self.ledger.waste.container_contents('D1')
        d2 = self.ledger.waste.container_contents('D2')
        self.assertEqual(d1['net_kg'], 6)
        self.assertEqual(d2['net_kg'], 3)
        self.assertEqual(
            [i['batch'] for i in d1['items']], ['w1'])
        self.assertEqual(
            [i['batch'] for i in d2['items']], ['w2'])
        self.assertEqual(self.ledger.waste.batches['w2']['location'], 'D2')

    def test_unsigned_manifest_cannot_use_correction_flow(self):
        # 签收前的分类变化只能拆封重签，禁止预开更正单。
        events = stream(self.data, 'solvent_confirmed_split_before_pickup')
        events.append({
            'subsystem': 'workshop-waste', 'id': 'bad-correct', 'subject': 'D1',
            'action': 'correct_issue', 'at': '2026-11-20T12:00:00', 'version': 1,
            'data': {'manifest_id': 'MF-X', 'linked_from': 'MF-ORIG',
                     'container': 'D1', 'capacity_kg': 10, 'museum_party': '馆员-赵宁'},
        })
        with self.assertRaises(ManifestError):
            PickupLedger(events)


class InvariantTest(unittest.TestCase):
    """物理账本的横切不变量。"""

    def test_batch_cannot_exist_in_two_containers(self):
        data = load_fixture()
        events = list(data['event_streams']['prologue'])
        # w2 已在 D1，再 place 一次必须拒绝。
        events.append({
            'subsystem': 'workshop-waste', 'id': 'dup-place', 'subject': 'w2',
            'action': 'place', 'at': '2026-11-20T12:00:00', 'version': 1,
            'data': {'batch': 'w2', 'container': 'D2', 'kg': 3, 'gross_kg': 3.8},
        })
        with self.assertRaises(LedgerError):
            WasteLedger(events)

    def test_sealed_container_rejects_new_placements(self):
        data = load_fixture()
        events = list(data['event_streams']['prologue'])
        events.append({
            'subsystem': 'workshop-waste', 'id': 'sealed-place', 'subject': 'w1',
            'action': 'place', 'at': '2026-11-20T12:00:00', 'version': 1,
            'data': {'batch': 'w1', 'container': 'D1', 'kg': 1, 'gross_kg': 11.0},
        })
        with self.assertRaises(LedgerError):
            WasteLedger(events)

    def test_disputed_dual_evidence_quarantines_batch(self):
        data = load_fixture()
        events = list(data['event_streams']['prologue'])
        events.extend([
            {'subsystem': 'workshop-waste', 'id': 'd-sup', 'subject': 'w2',
             'action': 'classify', 'at': '2026-11-20T10:00:00', 'version': 1,
             'data': {'batch': 'w2', 'result': 'solvent', 'basis': 'supplier-cert'}},
            {'subsystem': 'workshop-waste', 'id': 'd-inh', 'subject': 'w2',
             'action': 'classify', 'at': '2026-11-20T10:30:00', 'version': 1,
             'data': {'batch': 'w2', 'result': 'water-based', 'basis': 'inhouse-review'}},
        ])
        ledger = PickupLedger(events)
        self.assertEqual(ledger.waste.batches['w2']['classification'], 'disputed')
        self.assertEqual(
            [b['batch'] for b in ledger.waste.quarantine_batches()], ['w2'])
        self.assertFalse(ledger.delivery_status('D1', 10)['ok'])

    def test_single_evidence_is_not_certification(self):
        data = load_fixture()
        events = list(data['event_streams']['prologue'])
        events.append({
            'subsystem': 'workshop-waste', 'id': 'only-supplier', 'subject': 'w2',
            'action': 'classify', 'at': '2026-11-20T10:00:00', 'version': 1,
            'data': {'batch': 'w2', 'result': 'water-based', 'basis': 'supplier-cert'},
        })
        ledger = PickupLedger(events)
        self.assertEqual(ledger.waste.batches['w2']['classification'], 'pending')
        self.assertFalse(ledger.delivery_status('D1', 10)['ok'])

    def test_replay_is_deterministic(self):
        data = load_fixture()
        events = stream(data, 'solvent_confirmed_after_driver_signed')
        first = PickupLedger(events).report()
        second = PickupLedger(events).report()
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                         json.dumps(second, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    unittest.main()
