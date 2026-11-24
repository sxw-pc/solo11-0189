import copy
import json
import unittest
from pathlib import Path

from bookconserve.reader_requests import (
    accepted_hours,
    active_bans,
    arrival_hour,
    arrivals,
    attendance,
    consent_covers,
    for_volume,
    is_cancelled,
    is_qualified,
)
from bookconserve.reading_slots import (
    chain_cells,
    check_assignment,
    confirmations,
    generated_task_cells,
    issue_schedule,
    legal_schedules,
    replan,
)

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
FIXTURE = FIXTURES / 'reading-room.json'


def load_room(scenario=None):
    data = json.loads(FIXTURE.read_text(encoding='utf-8'))
    if scenario is None:
        room = data
    else:
        room = copy.deepcopy(data)
        room.update(copy.deepcopy(data['scenarios'][scenario]))
    room.pop('scenarios', None)
    return room


def booking(plan, request_id):
    return next(item for item in plan['bookings']
                if item['request_id'] == request_id)


PLAN_A = {'reader-A': 10, 'reader-B': 12}
PLAN_B = {'reader-B': 10, 'reader-A': 12}


class ReaderBookingSeedTest(unittest.TestCase):
    def setUp(self):
        self.room = load_room()

    # ---- 夹具隔离：同卷同小时两个请求都具备阅览资格 ----
    def test_same_volume_requests_are_isolated_and_qualified(self):
        rows = for_volume(self.room['requests'], 'MS-41')
        self.assertEqual([row['id'] for row in rows],
                         ['reader-A', 'reader-B'])
        self.assertTrue(all(is_qualified(row) for row in rows))
        self.assertEqual(sorted(accepted_hours(rows[0])), [10, 12])

    # ---- 两种合规排程，且只有这两种最大排程 ----
    def test_exactly_two_legal_schedules_with_matching_notices(self):
        schedules = legal_schedules(self.room)
        self.assertEqual(schedules, [PLAN_A, PLAN_B])
        plan_a = issue_schedule(self.room, PLAN_A)
        plan_b = issue_schedule(self.room, PLAN_B)
        # 同一原件同一时刻只能在一个桌位：请求小时上只有一方仍为 True
        self.assertEqual(confirmations(self.room, plan_a),
                         {'reader-A': True, 'reader-B': False})
        self.assertEqual(confirmations(self.room, plan_b),
                         {'reader-A': False, 'reader-B': True})
        # 被改期一方拿到的是 12 时确认，而不是仍显示 10 时已确认
        self.assertEqual(booking(plan_a, 'reader-B')['hour'], 12)
        self.assertTrue(booking(plan_a, 'reader-B')['rescheduled'])
        self.assertEqual(booking(plan_b, 'reader-A')['hour'], 12)
        notices_a = {n['request_id']: n['text'] for n in plan_a['notices']}
        self.assertIn('10:00-11:00', notices_a['reader-A'])
        self.assertIn('改签发 12:00-13:00', notices_a['reader-B'])
        notices_b = {n['request_id']: n['text'] for n in plan_b['notices']}
        self.assertIn('10:00-11:00', notices_b['reader-B'])
        self.assertIn('改签发 12:00-13:00', notices_b['reader-A'])

    # ---- 避光恢复窗：连续两个开放小时不能当两次预约 ----
    def test_one_hour_dark_recovery_between_readings(self):
        self.assertEqual(chain_cells(10), (9, 10, 11))
        cells = generated_task_cells(
            [{'hour': 10}, {'hour': 12}])
        kinds = {cell['hour']: cell['kind'] for cell in cells}
        self.assertEqual(kinds, {9: 'delivery', 10: 'reading',
                                 11: 'turnover', 12: 'reading',
                                 13: 'collection'})
        # 11 时是前一次翻阅的避光恢复窗
        plan_a = issue_schedule(self.room, PLAN_A)
        self.assertEqual(plan_a['recovery_hours'], [11, 13])
        self.assertIn(11, plan_a['librarian_cells'])
        # 把两次翻阅压进连续开放小时必然被判不可行
        reasons = check_assignment(
            self.room, {'reader-A': 10, 'reader-B': 11})
        self.assertTrue(any('避光恢复' in item['reason'] for item in reasons))
        # 只按开放小时去重的朴素结果同样不可行
        self.assertTrue(check_assignment(
            self.room, {'reader-A': 10, 'reader-B': 10}))

    # ---- 反例：11 时不可移动交接使表面空闲的 12 时无法履约 ----
    def test_fixed_librarian_handover_blocks_chain_feasibility(self):
        room = load_room('handover_at_11')
        self.assertEqual(legal_schedules(room), [])
        # 无合法排程时，任何一位读者都不能在其 10 时请求上显示已确认
        self.assertEqual(confirmations(room),
                         {'reader-A': False, 'reader-B': False})
        plan_a_reasons = check_assignment(room, PLAN_A)
        self.assertTrue(any('不可移动任务' in item['reason']
                            and item['hour'] == 11
                            for item in plan_a_reasons))
        plan_b_reasons = check_assignment(room, PLAN_B)
        self.assertTrue(any('不可移动任务' in item['reason']
                            for item in plan_b_reasons))
        # 后到请求机械推后一格同样过不了取送/恢复时序
        self.assertTrue(check_assignment(
            room, {'reader-A': 10, 'reader-B': 11}))
        # 单取 11 时也不可行：其取送单元正好撞上交接任务
        self.assertTrue(check_assignment(room, {'reader-A': 11}))

    # ---- 到场签收与桌位占用：无预订到场不能伪造签收 ----
    def test_arrival_sign_for_and_desk_occupancy(self):
        room = load_room('late_arrivals')
        plan_a = issue_schedule(room, PLAN_A)
        self.assertEqual(attendance(booking(plan_a, 'reader-A'),
                                    room['events']), 'on_time')
        self.assertEqual(attendance(booking(plan_a, 'reader-B'),
                                    room['events']), 'pending')
        # reader-C 无任何预订，先/晚到场都只能记 unscheduled，不生成实物记录
        self.assertEqual(booking(plan_a, 'reader-C')['status'], 'waitlisted')
        self.assertEqual(attendance(
            {'status': 'waitlisted', 'request_id': 'reader-C'},
            room['events']), 'unscheduled')
        self.assertNotIn('reader-C',
                         {entry.get('request_id') for entry in plan_a['usage']})
        # 逐小时桌位占用：原件在 10 时与 12 时分别落到具体桌位
        desks = {(entry['hour'], entry['request_id']): entry['desk']
                 for entry in plan_a['desk_occupancy']}
        self.assertEqual(desks[(10, 'reader-A')], 'desk-1')
        self.assertEqual(desks[(12, 'reader-B')], 'desk-2')
        readings = [entry for entry in plan_a['usage']
                    if entry['kind'] == 'reading']
        self.assertEqual(len(readings), 2)
        self.assertEqual(len({entry['desk'] for entry in readings}), 2)

    def test_late_arrival_keeps_original_commitment(self):
        room = load_room('late_arrivals')
        plan_a = issue_schedule(room, PLAN_A)
        # 迟到补录不能把已通知时段静默互换；候补者晚到也不能顶占已承诺桌位
        replanned = replan(room, plan_a, room['events'], now_hour=10)
        self.assertEqual(booking(replanned, 'reader-A')['hour'], 10)
        self.assertEqual(booking(replanned, 'reader-B')['hour'], 12)
        self.assertEqual(booking(replanned, 'reader-C')['status'], 'waitlisted')
        self.assertEqual(attendance(booking(replanned, 'reader-A'),
                                    room['events']), 'on_time')
        self.assertEqual(attendance(booking(replanned, 'reader-C'),
                                    room['events']), 'unscheduled')
        self.assertTrue(booking(replanned, 'reader-A')['pinned'])
        self.assertEqual(arrival_hour(arrivals(room['events'])['reader-A']),
                         10 + 10 / 60.0)

    # ---- 取消：原承诺留痕，仅未开始时段释放并由候补补位 ----
    def test_cancellation_releases_only_unstarted_slot(self):
        room = load_room('cancellation')
        plan_a = issue_schedule(self.room, PLAN_A)
        self.assertTrue(is_cancelled('reader-A', room['events']))
        replanned = replan(room, plan_a, room['events'], now_hour=9)
        # reader-A 的 10 时承诺留痕取消；reader-B 已通知的 12 时不移动
        self.assertEqual(booking(replanned, 'reader-B')['hour'], 12)
        canceled = booking(replanned, 'reader-A')
        self.assertEqual(canceled['status'], 'waitlisted')
        self.assertEqual(canceled['cancelled_hour'], 10)
        self.assertTrue(any(notice['kind'] == 'cancelled'
                            for notice in replanned['notices']))
        # 已开始时段的取消无效，承诺仍固定
        locked = replan(self.room, issue_schedule(self.room, PLAN_A),
                        room['events'], now_hour=10)
        self.assertEqual(booking(locked, 'reader-A')['status'], 'confirmed')
        self.assertEqual(booking(locked, 'reader-A')['hour'], 10)

    # ---- 禁阅：相关预订全部转候补，停止取送且不得伪造到场 ----
    def test_reinspection_ban_moves_all_bookings_to_waitlist(self):
        room = load_room('reinspection_ban')
        self.assertTrue(active_bans(room['events'], 'MS-41'))
        plan_a = issue_schedule(self.room, PLAN_A)
        banned = replan(room, plan_a, room['events'], now_hour=9)
        for request_id in ('reader-A', 'reader-B'):
            item = booking(banned, request_id)
            self.assertEqual(item['status'], 'waitlisted')
            self.assertIsNone(item['hour'])
            # 无到场事件时不得出现任何签收状态
            self.assertEqual(item['attendance'], 'none')
        self.assertEqual(banned['librarian_cells'], [])
        self.assertEqual(banned['desk_occupancy'], [])
        self.assertTrue(all(entry.get('suspended')
                            for entry in banned['usage']))
        self.assertTrue(all(notice['kind'] == 'banned'
                            for notice in banned['notices']))

    # ---- 桌位容量与同卷互斥：一册原件同一时刻只能占一个桌位 ----
    def test_desk_capacity_enforced_per_hour(self):
        # 同卷两位读者即便各有桌位，也不能在同一小时占用原件
        reasons = check_assignment(
            self.room, {'reader-A': 10, 'reader-B': 10})
        self.assertTrue(any(item['reason'] == '一册原件同一时刻只能在一个阅览桌位'
                            for item in reasons))
        # 单桌房间里，不同小时的两次翻阅轮流使用同一桌位
        single_desk = copy.deepcopy(self.room)
        single_desk['desks'] = [{'id': 'desk-1'}]
        plan = issue_schedule(single_desk, PLAN_A)
        self.assertEqual({entry['desk'] for entry in plan['desk_occupancy']},
                         {'desk-1'})
        # 桌位不足作为独立维度也能被识别（构造同小时超员分配）
        crowded = copy.deepcopy(single_desk)
        crowded['requests'] = [
            {'id': 'r1', 'volume': 'MS-41', 'hour': 10, 'duration': 1,
             'qualified': True, 'accepted_hours': []},
            {'id': 'r2', 'volume': 'MS-41', 'hour': 10, 'duration': 1,
             'qualified': True, 'accepted_hours': []},
        ]
        self.assertTrue(any('桌位' in item['reason']
                            for item in check_assignment(
                                crowded, {'r1': 10, 'r2': 10})))

    # ---- 读者同意是改期的唯一授权 ----
    def test_consent_required_for_reschedule(self):
        request = next(row for row in self.room['requests']
                       if row['id'] == 'reader-A')
        self.assertTrue(consent_covers(request, 10))
        self.assertTrue(consent_covers(request, 12))
        self.assertFalse(consent_covers(request, 11))
        no_consent = copy.deepcopy(self.room)
        no_consent['requests'][0]['accepted_hours'] = []
        no_consent['requests'][1]['accepted_hours'] = []
        schedules = legal_schedules(no_consent)
        # 无人同意改期时，只能单方在 10 时阅览，另一方候补
        self.assertEqual(schedules, [{'reader-A': 10}, {'reader-B': 10}])

    # ---- 既有修复账本夹具与排程模块并存不回退 ----
    def test_existing_repair_fixtures_still_evaluate(self):
        from bookconserve.policy import calculate
        for phase in ('ok', 'bad'):
            rows = []
            for subsystem in ('mandate', 'treatment', 'inspection'):
                rows.extend(json.loads(
                    (FIXTURES / f'{subsystem}-{phase}.json').read_text(
                        encoding='utf-8')))
            entities, decisions = calculate(rows)
            self.assertTrue(entities)
            self.assertGreaterEqual(len(decisions), 10)


if __name__ == '__main__':
    unittest.main()
