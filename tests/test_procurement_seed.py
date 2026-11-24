import copy
import json
import unittest
from pathlib import Path

from bookconserve.supplier_offers import (
    offers_for, certificate_matches, delivery_modes, feasible_modes,
    can_lock, quote_is_valid, lot_is_usable,
)
from bookconserve.purchase_plan import (
    plan_routes, choose, explain_blocks, usable_stock, confirm_supply, receive,
)
from bookconserve.policy import calculate

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'tissue-offers.json'


def load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def demand(data, work_order):
    return next(d for d in data['demands'] if d['work_order'] == work_order)


def route(routes, route_id):
    return next(r for r in routes if r['route_id'] == route_id)


class ProcurementSeedTest(unittest.TestCase):
    def setUp(self):
        self.data = load()
        self.offers = self.data['offers']
        self.ms41 = [demand(self.data, 'MS-41')]
        self.ms42 = [demand(self.data, 'MS-42')]
        self.both = self.data['demands']
        self.as_of = self.data['as_of']

    # 1. 证书闸：未核验/克重不符的批次不得订作等效材料
    def test_certificate_gate_rejects_unverified_mismatched_lot(self):
        v1, v2, v3 = self.offers
        self.assertTrue(certificate_matches(v1, 'japanese-tissue-12gsm', 12))
        self.assertTrue(certificate_matches(v2, 'japanese-tissue-12gsm', 12))
        self.assertFalse(certificate_matches(v3, 'japanese-tissue-12gsm', 12))
        # 仅按材料代码粗筛会混入 V3；带克重的证书核验只留 V1/V2
        self.assertEqual(
            {o['id'] for o in offers_for(self.offers, 'japanese-tissue-12gsm')},
            {'V1', 'V2', 'V3'})
        self.assertEqual(
            {o['id'] for o in offers_for(self.offers, 'japanese-tissue-12gsm', 12)},
            {'V1', 'V2'})
        routes = plan_routes(self.offers, self.ms41, as_of=self.as_of)
        self.assertNotIn('V3', {r['route_id'].split(':')[0] for r in routes})
        blocks = explain_blocks(self.offers, self.ms41, as_of=self.as_of)
        self.assertIn(
            '规格', next(b['reason'] for b in blocks if b['offer_id'] == 'V3'))

    # 2. 两条获授权路线：V1 加急 10 张 600 元 vs V2 常规 5 张 750 元
    def test_ms41_two_authorized_routes_with_full_ledger(self):
        routes = plan_routes(self.offers, self.ms41, as_of=self.as_of)
        self.assertEqual([r['route_id'] for r in routes],
                         ['V1:expedite', 'V2:regular'])
        # V1 常规 5 月 5 日晚于 5 月 3 日工期，不得作为路线出现
        self.assertNotIn('V1:regular', [r['route_id'] for r in routes])

        v1 = route(routes, 'V1:expedite')
        po = v1['purchase_orders'][0]
        self.assertEqual(po['arrival'], '2026-05-02')
        self.assertEqual(po['quantity'], 10)
        self.assertEqual(po['minimum'], 10)
        self.assertEqual(po['goods_value'], 500)
        self.assertEqual(po['expedite_fee'], 100)
        self.assertEqual(po['total_cash'], 600)
        self.assertEqual(v1['total_cash'], 600)
        self.assertEqual(po['lot'], 'LOT-V1-2604')
        self.assertEqual(po['certificate_doc'], 'CERT-V1-12G-2604')
        # MS-41 只领 5 张：整批多买不能全记到 MS-41 消耗
        ms41_req = [r for r in v1['requisitions'] if r['work_order'] == 'MS-41']
        self.assertEqual(len(ms41_req), 1)
        self.assertEqual(ms41_req[0]['quantity'], 5)
        self.assertEqual(ms41_req[0]['source'], 'purchase')
        self.assertEqual(ms41_req[0]['po_id'], po['po_id'])
        # 余 5 张独立入库存，按批次单价计存货价值，加急费不摊入余料
        self.assertEqual(len(v1['surplus']), 1)
        surplus = v1['surplus'][0]
        self.assertEqual(surplus['quantity'], 5)
        self.assertEqual(surplus['lot'], 'LOT-V1-2604')
        self.assertEqual(surplus['carrying_value'], 250)

        v2 = route(routes, 'V2:regular')
        po2 = v2['purchase_orders'][0]
        self.assertEqual(po2['arrival'], '2026-05-01')
        self.assertEqual(po2['quantity'], 5)
        self.assertEqual(po2['goods_value'], 750)
        self.assertEqual(po2['expedite_fee'], 0)
        self.assertEqual(v2['total_cash'], 750)
        self.assertEqual(sum(r['quantity'] for r in v2['requisitions']), 5)
        self.assertEqual(v2['surplus'], [])

        # 显式政策选择器取的是硬约束通过后的最低总现金，不是最低单价
        self.assertEqual(choose(self.offers, self.ms41, as_of=self.as_of),
                         'V1:expedite')

    # 3. MOQ 与已确认运力联合锁定，局部确认不得当全单交付
    def test_moq_and_confirmed_capacity_joint_lock(self):
        v1 = next(o for o in self.offers if o['id'] == 'V1')
        self.assertTrue(can_lock(v1, 'expedite', 10, '2026-05-03'))
        self.assertFalse(can_lock(v1, 'expedite', 5, '2026-05-03'))   # 低于 MOQ
        self.assertFalse(can_lock(v1, 'expedite', 11, '2026-05-03'))  # 超确认运力
        self.assertFalse(can_lock(v1, 'regular', 10, '2026-05-03'))   # 到货晚于工期
        po = route(plan_routes(self.offers, self.ms41, as_of=self.as_of),
                   'V1:expedite')['purchase_orders'][0]
        partial = confirm_supply(po, 8)
        self.assertFalse(partial['locked'])
        self.assertEqual(partial['shortfall'], 2)
        self.assertTrue(confirm_supply(po, 10)['locked'])
        blocks = explain_blocks(self.offers, self.ms41, as_of=self.as_of)
        self.assertIn('运力到货晚于工单工期',
                      next(b['reason'] for b in blocks
                           if b['offer_id'] == 'V1' and b['mode'] == 'regular'))

    # 4. 加急是独立运力，不是零费用开关
    def test_expedite_is_separate_capability_with_fee(self):
        v1 = next(o for o in self.offers if o['id'] == 'V1')
        v2 = next(o for o in self.offers if o['id'] == 'V2')
        self.assertEqual([m['mode'] for m in delivery_modes(v1)],
                         ['regular', 'expedite'])
        # V2 加急确认运力为 0，不得呈现虚假加急运力
        self.assertEqual([m['mode'] for m in delivery_modes(v2)], ['regular'])
        self.assertEqual(feasible_modes(v2, '2026-05-03')[0]['arrival'],
                         '2026-05-01')
        po = route(plan_routes(self.offers, self.ms41, as_of=self.as_of),
                   'V1:expedite')['purchase_orders'][0]
        self.assertEqual(po['mode'], 'expedite')
        self.assertEqual(po['total_cash'] - po['goods_value'], 100)

    # 5. 跨工单余料共同采购：独立领用链与归属
    def test_cross_workorder_surplus_reuse_keeps_independent_chain(self):
        first = route(plan_routes(self.offers, self.ms41, as_of=self.as_of),
                      'V1:expedite')
        surplus = first['surplus'][0]
        ms41_chain = first['requisitions'][0]['chain_id']

        # MS-42 用 MS-41 余料：纯库存路线，零现金，独立 RC 链
        reuse = plan_routes(self.offers, self.ms42, stock=[surplus],
                            as_of=self.as_of)
        self.assertEqual([r['route_id'] for r in reuse], ['STOCK'])
        stock_route = reuse[0]
        self.assertEqual(stock_route['total_cash'], 0)
        self.assertEqual(stock_route['purchase_orders'], [])
        req = stock_route['requisitions'][0]
        self.assertEqual(req['work_order'], 'MS-42')
        self.assertEqual(req['source'], 'stock')
        self.assertEqual(req['lot'], 'LOT-V1-2604')
        self.assertIsNone(req['po_id'])
        self.assertNotEqual(req['chain_id'], ms41_chain)

        # 两单联合规划：V1 一张 10 张 PO 同时服务两单，两条独立领用链，无余料
        joint = plan_routes(self.offers, self.both, as_of=self.as_of)
        v1_joint = route(joint, 'V1:expedite')
        self.assertEqual(v1_joint['purchase_orders'][0]['quantity'], 10)
        self.assertEqual(
            sorted((r['work_order'], r['quantity'])
                   for r in v1_joint['requisitions']),
            [('MS-41', 5), ('MS-42', 5)])
        self.assertEqual(len({r['chain_id'] for r in v1_joint['requisitions']}), 2)
        self.assertEqual(v1_joint['surplus'], [])
        # V2 确认运力仅 5 张，联合需求 10 张：逐工单最便宜/MOQ 截断在此失效
        self.assertNotIn('V2:regular', [r['route_id'] for r in joint])

    # 6. 批次 5 月 4 日到期：不能给 5 月 6 日的第二单
    def test_expired_lot_cannot_serve_second_work_order(self):
        offers = copy.deepcopy(self.offers)
        v1 = next(o for o in offers if o['id'] == 'V1')
        v1['lot_expiry'] = '2026-05-04'
        self.assertTrue(lot_is_usable(v1, '2026-05-03'))
        self.assertFalse(lot_is_usable(v1, '2026-05-06'))

        # MS-41 仍可订（到货 5 月 2 日、领用 5 月 3 日均不晚于到期日）
        first = route(plan_routes(offers, self.ms41, as_of=self.as_of),
                      'V1:expedite')
        self.assertEqual(first['purchase_orders'][0]['quantity'], 10)
        stale_surplus = first['surplus'][0]
        # 余料对 MS-42 不可用
        self.assertEqual(
            usable_stock([stale_surplus], 'japanese-tissue-12gsm', 12,
                         '2026-05-06'), [])
        reuse = plan_routes(offers, self.ms42, stock=[stale_surplus],
                            as_of=self.as_of)
        self.assertNotIn('STOCK', [r['route_id'] for r in reuse])
        self.assertEqual(
            {r['route_id'] for r in reuse}, {'V2:regular'})

        # 联合需求下 V1 死于有效期、V2 死于确认运力：无可行路线，不得硬下单
        with self.assertRaises(ValueError):
            choose(offers, self.both, as_of=self.as_of)
        blocks = explain_blocks(offers, self.both, as_of=self.as_of)
        reasons = {(b['offer_id'], b['reason']) for b in blocks}
        self.assertTrue(any('到期' in reason for _, reason in reasons))
        self.assertTrue(any('确认运力不足' in reason for _, reason in reasons))

    # 7. 迟到/缺货签收：保留原合同，同供应商同批次补单，不静默换商
    def test_late_and_short_delivery_preserves_contract_and_replenishes(self):
        po = route(plan_routes(self.offers, self.ms41, as_of=self.as_of),
                   'V1:expedite')['purchase_orders'][0]

        on_time = receive(po, 10, '2026-05-02')
        self.assertEqual(on_time['receipt']['status'], 'on_time')
        self.assertIsNone(on_time['replenishment'])

        late = receive(po, 10, '2026-05-04')
        self.assertEqual(late['receipt']['status'], 'late')
        self.assertEqual(late['receipt']['contracted_arrival'], '2026-05-02')
        self.assertIsNone(late['replenishment'])

        short = receive(po, 8, '2026-05-02')
        self.assertEqual(short['receipt']['status'], 'short')
        rep = short['replenishment']
        self.assertEqual(rep['linked_po'], po['po_id'])
        self.assertEqual(rep['quantity'], 2)
        self.assertEqual(rep['supplier'], po['supplier'])
        self.assertEqual(rep['offer_id'], 'V1')
        self.assertEqual(rep['lot'], po['lot'])
        self.assertEqual(rep['serves'], po['serves'])

        with self.assertRaises(ValueError):
            receive(po, 11, '2026-05-02')

    # 8. 报价有效期：计划日晚于 V1 报价有效期时 V1 退出
    def test_quote_validity_window(self):
        v1 = next(o for o in self.offers if o['id'] == 'V1')
        self.assertTrue(quote_is_valid(v1, '2026-04-30'))
        self.assertFalse(quote_is_valid(v1, '2026-05-01'))
        routes = plan_routes(self.offers, self.ms41, as_of='2026-05-01')
        self.assertEqual([r['route_id'] for r in routes], ['V2:regular'])
        self.assertEqual(
            choose(self.offers, self.ms41, as_of='2026-05-01'),
            'V2:regular')

    # 9. 库存闸口：克重不符或已到期的余料不得被任何工单领用
    def test_stock_rejects_wrong_gsm_and_expired_lots(self):
        bad_gsm = {'material': 'japanese-tissue-12gsm', 'gsm': 9,
                   'lot': 'LOT-X', 'quantity': 5, 'lot_expiry': '2026-12-31'}
        stale = {'material': 'japanese-tissue-12gsm', 'gsm': 12,
                 'lot': 'LOT-Y', 'quantity': 5, 'lot_expiry': '2026-04-30'}
        good = {'material': 'japanese-tissue-12gsm', 'gsm': 12,
                'lot': 'LOT-Z', 'quantity': 5, 'lot_expiry': '2026-12-31'}
        self.assertEqual(
            usable_stock([bad_gsm, stale, good], 'japanese-tissue-12gsm',
                         12, '2026-05-03'),
            [good])
        routes = plan_routes(self.offers, self.ms41,
                             stock=[bad_gsm, stale], as_of=self.as_of)
        # 库存全部不可用，只能走采购路线且不得把不合格库存写进领用链
        self.assertNotIn('STOCK', [r['route_id'] for r in routes])
        self.assertTrue(all(not r['requisitions'] or
                            all(rq['source'] == 'purchase' for rq in r['requisitions'])
                            for r in routes))

    # 10. 原有修复夹具（委托/处理/复检三账）仍可重算
    def test_existing_conservation_fixtures_still_replay(self):
        rows = []
        for sub in ('mandate', 'treatment', 'inspection'):
            payload = json.loads(
                (ROOT / 'fixtures' / f'{sub}-ok.json').read_text(
                    encoding='utf-8'))
            self.assertIsInstance(payload, list)
            self.assertTrue(payload)
            rows.extend(payload)
        for phase in ('ok', 'bad'):
            for sub in ('mandate', 'treatment', 'inspection'):
                json.loads((ROOT / 'fixtures' / f'{sub}-{phase}.json').read_text(
                    encoding='utf-8'))
        entities, decisions = calculate(rows)
        self.assertGreaterEqual(len(decisions), 10)
        self.assertTrue(any(
            d[1] == '迟到复检是否撤销阶段验收' and d[2] == '验收'
            for d in decisions))


if __name__ == '__main__':
    unittest.main()
