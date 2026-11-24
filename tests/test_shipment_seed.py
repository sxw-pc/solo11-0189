import copy
import json
import unittest
from pathlib import Path

from bookconserve.route_capture import (
    accumulated_exposure,
    build_timeline,
    candidate_routes,
    find_resource_conflicts,
    late_sensor_receipts,
)
from bookconserve.shipment_quote import (
    evaluate_route,
    executable_plans,
    issue_order,
    requote_for_broken_seal,
    requote_for_late_receipt,
)

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'book-shipment.json'


def load_ledger():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def route(ledger, route_id):
    return next(item for item in ledger['routes'] if item['id'] == route_id)


class ShipmentSeedTest(unittest.TestCase):
    def setUp(self):
        self.ledger = load_ledger()

    # ---- 原有修复夹具的兼容性：候选仍按起终点收全部固定报价 ----
    def test_candidate_routes_still_collects_all_quotes(self):
        routes = candidate_routes(self.ledger['routes'],
                                  self.ledger['origin'],
                                  self.ledger['destination'])
        self.assertEqual([item['id'] for item in routes],
                         ['bare-direct', 'cooled-direct', 'sealed-relay',
                          'relay-lag-fail'])
        self.assertEqual(len(routes), 4)

    # ---- 合法路线一：cooled-direct，800 元 / 12 时 / 实测暴露 20 ----
    def test_cooled_direct_plan_is_verifiable(self):
        result = evaluate_route(route(self.ledger, 'cooled-direct'), self.ledger)
        self.assertTrue(result['feasible'], result['reasons'])
        self.assertEqual(result['actual_exposure'], 20)
        self.assertEqual(result['arrival_hour'], 12)
        self.assertEqual(result['price'], 800)
        self.assertIn('eq-van-cooled-A-cert', result['evidence'])
        self.assertIn('win-museum-b-09-17', result['evidence'])
        order = issue_order(self.ledger, 'cooled-direct', issue_hour=7)
        self.assertEqual(order['actual_exposure'], 20)
        self.assertEqual(order['arrival_hour'], 12)
        self.assertEqual(order['price'], 800)
        self.assertEqual(order['status'], 'issued')
        self.assertRegex(order['fingerprint'], r'^[0-9a-f]{64}$')

    # ---- 合法路线二：sealed-relay，650 元 / 16 时 / 累计暴露 35 ----
    def test_sealed_relay_plan_is_verifiable(self):
        result = evaluate_route(route(self.ledger, 'sealed-relay'), self.ledger)
        self.assertTrue(result['feasible'], result['reasons'])
        self.assertEqual(result['actual_exposure'], 35)
        self.assertEqual(result['arrival_hour'], 16)
        self.assertEqual(result['price'], 650)
        # 开箱/再封箱交接事件必须进入证据链
        self.assertIn('seal-relay-open-001', result['evidence'])
        self.assertIn('seal-relay-reseal-001', result['evidence'])
        order = issue_order(self.ledger, 'sealed-relay', issue_hour=7)
        self.assertEqual(order['handoffs'][0]['open_hour'], 11)
        self.assertEqual(order['handoffs'][0]['reseal_hour'], 12)
        self.assertEqual(order['handoffs'][0]['recovery_hours'], 2)
        self.assertEqual(order['handoffs'][0]['seal_open_event'],
                         'seal-relay-open-001')
        self.assertEqual(order['vehicles_and_transfers'][0]
                         ['resource_evidence'], 'eq-van-cooled-A-cert')
        self.assertIn('win-museum-b-09-17', order['evidence'])

    # ---- 两条合法计划同时可选：换更早到馆或省 150 元，而非按 dose 字段排序 ----
    def test_two_authorized_plans_with_tradeoff(self):
        plans = executable_plans(self.ledger)
        ids = {item['id']: item for item in plans['feasible']}
        self.assertEqual(set(ids), {'cooled-direct', 'sealed-relay'})
        self.assertEqual(ids['cooled-direct']['price'], 800)
        self.assertEqual(ids['sealed-relay']['price'], 650)
        self.assertIn('最早到馆', ids['cooled-direct']['tradeoff'])
        self.assertIn('节省 150 元', ids['sealed-relay']['tradeoff'])
        rejected = {item['id']: item for item in plans['rejected']}
        self.assertIn('bare-direct', rejected)
        self.assertIn('relay-lag-fail', rejected)
        # bare-direct 不是被简单删除：它仍被评估，且因实测暴露 120 超限被拒签
        self.assertFalse(rejected['bare-direct']['checks']
                         ['exposure_under_limit'])
        self.assertEqual(rejected['bare-direct']['actual_exposure'], 120)

    def test_choose_does_not_sort_by_supplier_dose(self):
        plans = executable_plans(self.ledger)
        # 反例路线供应商摘要 dose=30（最低），但不得出现在可签发集合
        self.assertNotIn('relay-lag-fail',
                         [item['id'] for item in plans['feasible']])
        feasible = executable_plans(self.ledger)['feasible']
        self.assertEqual(min(item['price'] for item in feasible), 650)

    # ---- 反例：两段各自低于 40，开箱后恢复滞后使连续累计超过 40 ----
    def test_open_box_recovery_lag_breaks_static_dose_assumption(self):
        lag = route(self.ledger, 'relay-lag-fail')
        first = accumulated_exposure(
            {'segments': [lag['segments'][0]], 'handoffs': [],
             'id': 'lag/s1'})[0]
        second = accumulated_exposure(
            {'segments': [lag['segments'][1]], 'handoffs': [],
             'id': 'lag/s2'})[0]
        self.assertLess(first, 40)
        self.assertLess(second, 40)
        # 供应商静态 dose 字段合计仅 30，静态可加边权会放行
        self.assertEqual(lag['moisture_dose'], 30)
        result = evaluate_route(lag, self.ledger)
        self.assertFalse(result['feasible'])
        self.assertFalse(result['checks']['exposure_under_limit'])
        self.assertEqual(result['actual_exposure'], 42)
        self.assertIsNotNone(result['dose_delta_vs_supplier'])
        self.assertEqual(result['dose_delta_vs_supplier'], 12)

    # ---- 再封箱后不得沿用上一段控湿状态 ----
    def test_resealed_box_restarts_recovery_instead_of_carryover(self):
        relay = route(self.ledger, 'sealed-relay')
        timeline = build_timeline(relay)
        states = {point['hour']: point['state'] for point in timeline}
        # 11 时开箱、12 时再封箱、13 时发车前处于恢复中
        self.assertEqual(states[11], 'open')
        self.assertEqual(states[12], 'recovery')
        self.assertEqual(states[13], 'recovery')
        # 13 时箱内 RH=55，高于设定值 53，证明控湿状态被重置后正在恢复
        point_13 = next(p for p in timeline if p['hour'] == 13)
        self.assertEqual(point_13['rh'], 55)
        # 供应商摘要不参与累计：篡改摘要不改变实测结果
        tampered = copy.deepcopy(relay)
        tampered['moisture_dose'] = 0
        self.assertEqual(
            accumulated_exposure(tampered)[0],
            accumulated_exposure(relay)[0])

    # ---- 重复占车：同一车箱时段重叠即冲突，首尾相接不冲突 ----
    def test_double_booking_of_same_vehicle_slot_is_rejected(self):
        clean = find_resource_conflicts(self.ledger['routes'],
                                        self.ledger['bookings'])
        self.assertEqual(clean, [])
        colliding = copy.deepcopy(self.ledger)
        colliding['bookings'].append({
            'id': 'bk-bad', 'resource': 'van-cooled-A',
            'start_hour': 9, 'end_hour': 11, 'route': 'other-volume',
            'evidence': 'bk-bad-slip'})
        conflicts = find_resource_conflicts(colliding['routes'],
                                            colliding['bookings'])
        self.assertTrue(any(item['resource'] == 'van-cooled-A'
                            for item in conflicts))
        result = evaluate_route(route(colliding, 'cooled-direct'), colliding)
        self.assertFalse(result['checks']['no_resource_double_booking'])
        # 首尾相接（8 时接 8 时）允许
        back_to_back = copy.deepcopy(self.ledger)
        back_to_back['bookings'].append({
            'id': 'bk-ok', 'resource': 'van-cooled-A',
            'start_hour': 12, 'end_hour': 13, 'route': 'other-volume',
            'evidence': 'bk-ok-slip'})
        self.assertEqual(
            find_resource_conflicts(back_to_back['routes'],
                                    back_to_back['bookings']), [])

    # ---- 到馆窗口：晚于 17 时拒签，17 时整仍在窗内 ----
    def test_receiving_window_deadline(self):
        late = copy.deepcopy(self.ledger)
        late_route = route(late, 'cooled-direct')
        late_route['arrival_hour'] = 18
        result = evaluate_route(late_route, late)
        self.assertFalse(result['checks']['arrival_in_window'])
        on_time = copy.deepcopy(self.ledger)
        route(on_time, 'cooled-direct')['arrival_hour'] = 17
        self.assertTrue(
            evaluate_route(route(on_time, 'cooled-direct'), on_time)
            ['checks']['arrival_in_window'])

    # ---- 迟到传感器回执：原单保留，重算超限后仅本卷重报价并给责任区间 ----
    def test_late_sensor_receipt_keeps_order_and_requotes_affected_volume(self):
        order = issue_order(self.ledger, 'sealed-relay', issue_hour=7)
        relay = route(self.ledger, 'sealed-relay')
        # 夹具内置的迟到回执：段内传感器 18 时才送达，7 时签发时不可得
        late = late_sensor_receipts(relay, issue_hour=7)
        self.assertIn('relay-s2-t15-late', [item['id'] for item in late])
        # 仅把段内那条迟到传感器作为重算补证
        late_samples = [item for item in late
                        if item['id'] == 'relay-s2-t15-late']
        # 签发视角剂量仍是 35，迟到件未污染基线
        self.assertEqual(order['actual_exposure'], 35)
        original_fingerprint = order['fingerprint']
        outcome = requote_for_late_receipt(self.ledger, order, late_samples)
        self.assertEqual(outcome['original_order']['fingerprint'],
                         original_fingerprint)
        self.assertEqual(outcome['original_order']['status'],
                         'retained-under-appeal')
        self.assertFalse(outcome['still_within_limit'])
        self.assertGreater(outcome['recomputed_exposure'], 40)
        self.assertEqual(outcome['liability_window']['from_hour'], 15)
        self.assertEqual(outcome['liability_window']['to_hour'], 15)
        self.assertFalse(outcome['other_volumes_affected'])
        # 重报价落到仍可行的最低价路线 cooled-direct（650 元自身已失效）
        self.assertEqual(outcome['requote']['route_id'], 'cooled-direct')
        self.assertEqual(outcome['requote']['price'], 800)

    # ---- 封箱破损：原单保留、责任区间为破损至再封箱、仅本卷重报价 ----
    def test_broken_seal_keeps_order_and_assigns_liability_window(self):
        order = issue_order(self.ledger, 'sealed-relay', issue_hour=7)
        breach = {
            'seal_event_id': 'seal-breach-001', 'detected_hour': 11,
            'reseal_hour': 12,
            'samples': [
                {'id': 'breach-t11', 'at_hour': 11, 'rh': 90},
                {'id': 'breach-t12', 'at_hour': 12, 'rh': 90}]}
        outcome = requote_for_broken_seal(self.ledger, order, breach)
        self.assertEqual(outcome['original_order']['status'],
                         'retained-under-appeal')
        self.assertEqual(outcome['liability_window']['from_hour'], 11)
        self.assertEqual(outcome['liability_window']['to_hour'], 12)
        self.assertEqual(outcome['liability_window']['seal_event'],
                         'seal-breach-001')
        self.assertFalse(outcome['other_volumes_affected'])
        self.assertFalse(outcome['still_within_limit'])
        self.assertEqual(outcome['requote']['route_id'], 'cooled-direct')

    # ---- 不可签发路线不能出运输单 ----
    def test_issue_order_refuses_infeasible_route(self):
        with self.assertRaises(ValueError):
            issue_order(self.ledger, 'bare-direct', issue_hour=7)
        with self.assertRaises(ValueError):
            issue_order(self.ledger, 'relay-lag-fail', issue_hour=7)

    # ---- 旧式三参数调用仍可工作 ----
    def test_legacy_three_argument_choose_signature(self):
        from bookconserve.shipment_quote import choose
        result = choose(self.ledger['routes'], self.ledger['origin'],
                        self.ledger['destination'])
        self.assertEqual({item['id'] for item in result['feasible']},
                         {'cooled-direct', 'sealed-relay'})


if __name__ == '__main__':
    unittest.main()
