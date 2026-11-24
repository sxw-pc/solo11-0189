import copy
import json
import unittest
from pathlib import Path

from bookconserve.bench_capacity import (
    CaptureConflict, ProcedureBlocked, ROUTE_ACTIVE, ROUTE_PASSIVE,
    open_ledger, recalibrate, remaining, supplement_evidence,
)
from bookconserve.shift_capture import (
    DEVICE_STOPPED, PASSIVE_DRY, PENDING_REVIEW, active_hours, bench_hours,
    capture_session, hours, pending_review_periods, schedule_consolidation,
    union_hours,
)

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'restorer-clocks.json'
DATE = '2026-11-24'

# 原始两行台面回盘：缺少设备时钟锚点与干燥开始签认。
RAW_BENCH_A = {"id": "bench-a", "volume": "V1", "restorer": "R1",
               "device": "camera", "start": 8, "end": 12}
RAW_BENCH_B = {"id": "bench-b", "volume": "V2", "restorer": "R1",
               "device": "timer", "start": 8, "end": 12}


def load_rows():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def route_a_evidence():
    """路线甲：V1 在 8-12 被动干燥，R1 同时主动处理 V2。"""
    return [
        {'id': 'e-clk-cam-a', 'type': 'clock_session', 'device': 'camera',
         'date': DATE, 'offset_hours': 0},
        {'id': 'e-clk-tmr-a', 'type': 'clock_session', 'device': 'timer',
         'date': DATE, 'offset_hours': 0},
        {'id': 'e-pres-a', 'type': 'presence', 'restorer': 'R1', 'date': DATE,
         'segments': [{'start': 8, 'end': 12, 'location': 'bench-b',
                       'volume': 'V2'}]},
        {'id': 'e-vid-v1-a', 'type': 'video', 'row_id': 'bench-a',
         'date': DATE, 'mode': PASSIVE_DRY, 'stage': 'dry',
         'start': 8, 'end': 12, 'restorer': 'R1'},
        {'id': 'e-vid-v2-a', 'type': 'video', 'row_id': 'bench-b',
         'date': DATE, 'mode': 'active', 'stage': 'clean',
         'start': 8, 'end': 12, 'restorer': 'R1'},
        {'id': 'e-env-v1', 'type': 'environment', 'volume': 'V1',
         'date': DATE, 'dry_ready_at': 12},
        {'id': 'e-sign-v1', 'type': 'sign_off', 'volume': 'V1',
         'date': DATE, 'stage': 'dry_review', 'at': 12},
    ]


def route_b_evidence():
    """路线乙：手持计时器慢四小时，两段主动工作 8-12、12-16 相接。"""
    return [
        {'id': 'e-clk-cam-b', 'type': 'clock_session', 'device': 'camera',
         'date': DATE, 'offset_hours': 0},
        {'id': 'e-clk-tmr-b', 'type': 'clock_session', 'device': 'timer',
         'date': DATE, 'offset_hours': 4},
        {'id': 'e-pres-b', 'type': 'presence', 'restorer': 'R1', 'date': DATE,
         'segments': [
             {'start': 8, 'end': 12, 'location': 'bench-a', 'volume': 'V1'},
             {'start': 12, 'end': 16, 'location': 'bench-b', 'volume': 'V2'}]},
        {'id': 'e-vid-v1-b', 'type': 'video', 'row_id': 'bench-a',
         'date': DATE, 'mode': 'active', 'stage': 'consolidation',
         'start': 8, 'end': 12, 'restorer': 'R1'},
        {'id': 'e-vid-v2-b', 'type': 'video', 'row_id': 'bench-b',
         'date': DATE, 'mode': 'active', 'stage': 'clean',
         'start': 12, 'end': 16, 'restorer': 'R1'},
    ]


class RestorerTimeSeedTest(unittest.TestCase):
    def test_parallel_receipts_consume_all_capacity(self):
        # 旧夹具基线：逐行求和的零剩余投影保持可复现。
        rows = load_rows()
        self.assertEqual(hours(rows), 8)
        self.assertEqual(remaining(rows), {'used': 8, 'remaining': 0})

    def test_raw_receipts_are_frozen(self):
        # 两路补证都必须保留原回盘，原始 JSON 不被改写。
        self.assertEqual(load_rows(), [RAW_BENCH_A, RAW_BENCH_B])

    def test_unanchored_rows_are_pending_not_personal_time(self):
        rows = load_rows()
        session = capture_session(rows, [])
        self.assertEqual(active_hours(session), 0)
        self.assertEqual(bench_hours(session), 8)
        pending = pending_review_periods(session)
        self.assertEqual({p['reason'] for p in pending}, {PENDING_REVIEW})
        self.assertEqual({p['row_id'] for p in pending}, {'bench-a', 'bench-b'})
        self.assertTrue(all(v['state'] == 'unanchored'
                            for v in session['volumes'].values()))

    def test_route_a_passive_drying_runs_parallel_with_active_work(self):
        rows = load_rows()
        session = capture_session(rows, route_a_evidence(), date=DATE)
        self.assertEqual(active_hours(session, 'R1'), 4)
        self.assertEqual(bench_hours(session), 8)
        v1, v2 = session['volumes']['V1'], session['volumes']['V2']
        self.assertEqual(v1['state'], 'ready_to_consolidate')
        self.assertEqual(v2['state'], 'active_work')
        # 被动干燥与主动清洁在物理时间上并行，个人工时不双计。
        passive = [(s['start'], s['end']) for s in session['passive_segments']]
        active = [(s['start'], s['end']) for s in session['active_segments']]
        self.assertEqual(passive, [(8, 12)])
        self.assertEqual(active, [(8, 12)])
        self.assertEqual(union_hours(active), 4)

    def test_route_a_remaining_four_and_consolidation_gated(self):
        rows = load_rows()
        ledger = open_ledger(rows, [], date=DATE)
        supplement_evidence(ledger, rows, route_a_evidence(),
                            ROUTE_PASSIVE, date=DATE)
        current = ledger['current']
        self.assertEqual(current['personal_account']['R1'],
                         {'used_hours': 4, 'remaining_hours': 4})
        self.assertEqual(current['bench_account']['occupied_hours'], 8)
        session = capture_session(rows, route_a_evidence(), date=DATE)
        with self.assertRaises(ProcedureBlocked):
            schedule_consolidation(session, 'V1', 11)
        self.assertEqual(schedule_consolidation(session, 'V1', 12)
                         ['allowed'], True)

    def test_route_b_slow_timer_makes_two_active_shifts_contiguous(self):
        rows = load_rows()
        session = capture_session(rows, route_b_evidence(), date=DATE)
        timer_row = next(r for r in session['rows'] if r['id'] == 'bench-b')
        self.assertEqual(timer_row['offset_hours'], 4)
        self.assertEqual((timer_row['start'], timer_row['end']), (12, 16))
        self.assertEqual(active_hours(session, 'R1'), 8)
        self.assertEqual(bench_hours(session), 8)
        self.assertEqual(session['volumes']['V1']['state'], 'consolidated')
        self.assertEqual(session['volumes']['V1']['consolidated_at'], 12)

    def test_route_b_remaining_zero(self):
        rows = load_rows()
        ledger = open_ledger(rows, [], date=DATE)
        supplement_evidence(ledger, rows, route_b_evidence(),
                            ROUTE_ACTIVE, date=DATE)
        current = ledger['current']
        self.assertEqual(current['personal_account']['R1'],
                         {'used_hours': 8, 'remaining_hours': 0})
        self.assertEqual(current['route'], ROUTE_ACTIVE)

    def test_overlapping_active_without_session_offset_rejected(self):
        # 不能逐条把重叠移开：两段主动重叠且无会话级校时即为冲突。
        rows = load_rows()
        bad = [e for e in route_b_evidence() if e['type'] != 'clock_session']
        with self.assertRaises(CaptureConflict):
            capture_session(rows, bad, date=DATE)

    def test_clock_offset_is_session_shared_for_one_device(self):
        # 同一设备两条段，两条互相矛盾的设备校时不能并存。
        rows = [RAW_BENCH_A,
                {'id': 'bench-b', 'volume': 'V2', 'restorer': 'R1',
                 'device': 'timer', 'start': 8, 'end': 12},
                {'id': 'bench-c', 'volume': 'V3', 'restorer': 'R1',
                 'device': 'timer', 'start': 13, 'end': 17}]
        evidence = [
            {'id': 't1', 'type': 'clock_session', 'device': 'timer',
             'date': DATE, 'offset_hours': 4},
            {'id': 't2', 'type': 'clock_session', 'device': 'timer',
             'date': DATE, 'offset_hours': 0},
        ]
        with self.assertRaises(CaptureConflict):
            capture_session(rows, evidence, date=DATE)

    def test_offset_applies_to_every_row_of_the_device(self):
        # 校时是整次会话共用：timer 慢四小时同时作用于该设备全部段。
        rows = [
            {'id': 't-row-1', 'volume': 'V2', 'restorer': 'R1',
             'device': 'timer', 'start': 8, 'end': 10},
            {'id': 't-row-2', 'volume': 'V3', 'restorer': 'R1',
             'device': 'timer', 'start': 10, 'end': 12},
        ]
        evidence = [{'id': 'clk', 'type': 'clock_session', 'device': 'timer',
                     'date': DATE, 'offset_hours': 4}]
        session = capture_session(rows, evidence, date=DATE)
        self.assertEqual(
            [(r['id'], r['start'], r['end']) for r in session['rows']],
            [('t-row-1', 12, 14), ('t-row-2', 14, 16)])
        self.assertTrue(all(r['offset_hours'] == 4 for r in session['rows']))

    def test_downtime_marks_pending_and_blocks_active_claim(self):
        rows = load_rows()
        evidence = [
            {'id': 'clk', 'type': 'clock_session', 'device': 'camera',
             'date': DATE, 'offset_hours': 0, 'downtime': [(10, 11)]},
        ]
        session = capture_session(rows, evidence, date=DATE)
        pending = pending_review_periods(session)
        stopped = next(p for p in pending if p['row_id'] == 'bench-a')
        self.assertEqual(stopped['reason'], DEVICE_STOPPED)
        # 停机段内不能用视频把该段钉为主动操作。
        evidence.append({'id': 'vid', 'type': 'video', 'row_id': 'bench-a',
                         'date': DATE, 'mode': 'active', 'stage': 'consolidation',
                         'start': 8, 'end': 12, 'restorer': 'R1'})
        evidence.append({'id': 'pres', 'type': 'presence', 'restorer': 'R1',
                         'date': DATE,
                         'segments': [{'start': 8, 'end': 12,
                                       'location': 'bench-a', 'volume': 'V1'}]})
        with self.assertRaises(CaptureConflict):
            capture_session(rows, evidence, date=DATE)

    def test_zero_personal_remaining_does_not_unblock_drying_volume(self):
        # R1 已满八小时主动工时，但 V3 仍在被动干燥且无复核签认，不得加固。
        rows = [
            {'id': 'b3', 'volume': 'V3', 'restorer': 'R1', 'device': 'camera',
             'start': 8, 'end': 12},
            {'id': 'b4', 'volume': 'V4', 'restorer': 'R1', 'device': 'timer',
             'start': 8, 'end': 12},
            {'id': 'b5', 'volume': 'V5', 'restorer': 'R1', 'device': 'timer',
             'start': 12, 'end': 16},
        ]
        evidence = [
            {'id': 'p', 'type': 'presence', 'restorer': 'R1', 'date': DATE,
             'segments': [
                 {'start': 8, 'end': 12, 'location': 'bench-b', 'volume': 'V4'},
                 {'start': 12, 'end': 16, 'location': 'bench-b',
                  'volume': 'V5'}]},
            {'id': 'v3', 'type': 'video', 'row_id': 'b3', 'date': DATE,
             'mode': PASSIVE_DRY, 'stage': 'dry', 'start': 8, 'end': 12},
            {'id': 'v4', 'type': 'video', 'row_id': 'b4', 'date': DATE,
             'mode': 'active', 'stage': 'clean', 'start': 8, 'end': 12},
            {'id': 'v5', 'type': 'video', 'row_id': 'b5', 'date': DATE,
             'mode': 'active', 'stage': 'clean', 'start': 12, 'end': 16},
            {'id': 'env3', 'type': 'environment', 'volume': 'V3',
             'date': DATE, 'dry_ready_at': 12},
        ]
        session = capture_session(rows, evidence, date=DATE)
        self.assertEqual(active_hours(session, 'R1'), 8)
        self.assertEqual(session['volumes']['V3']['state'],
                         'awaiting_dry_review')
        with self.assertRaises(ProcedureBlocked):
            schedule_consolidation(session, 'V3', 13)

    def test_presence_conflict_and_missing_presence_rejected(self):
        rows = load_rows()
        evidence = route_b_evidence()
        evidence.append({'id': 'ghost', 'type': 'presence', 'restorer': 'R1',
                         'date': DATE,
                         'segments': [{'start': 9, 'end': 10,
                                       'location': 'bench-z', 'volume': 'V9'}]})
        with self.assertRaises(CaptureConflict):
            capture_session(rows, evidence, date=DATE)
        no_presence = [e for e in route_b_evidence()
                       if e['type'] != 'presence']
        with self.assertRaises(CaptureConflict):
            capture_session(rows, no_presence, date=DATE)

    def test_versioned_ledger_freezes_legacy_zero_remaining(self):
        rows = load_rows()
        ledger = open_ledger(rows, [], date=DATE)
        revision1 = ledger['revisions'][0]
        self.assertEqual(revision1['revision'], 1)
        self.assertEqual(revision1['used'], 8)
        self.assertEqual(revision1['remaining'], 0)
        frozen = copy.deepcopy(revision1)
        supplement_evidence(ledger, rows, route_a_evidence(),
                            ROUTE_PASSIVE, date=DATE)
        self.assertEqual(ledger['revisions'][0], frozen)
        self.assertEqual(len(ledger['revisions']), 3)
        self.assertEqual(ledger['current']['revision'], 3)
        # 两路补证共享同一份不可变原回盘。
        with self.assertRaises(ValueError):
            tampered = [dict(r, end=11) for r in rows]
            supplement_evidence(open_ledger(rows, [], date=DATE), tampered,
                                route_a_evidence(), ROUTE_PASSIVE, date=DATE)

    def test_late_calibration_only_recomputes_affected_date(self):
        other_date = '2026-11-25'
        rows_today = load_rows()
        rows_other = [dict(r, date=other_date) for r in load_rows()]
        ledgers = {
            DATE: open_ledger(rows_today, [], date=DATE),
            other_date: open_ledger(rows_other, [], date=other_date),
        }
        untouched_revisions = ledgers[other_date]['revisions']
        snapshot = copy.deepcopy(ledgers[other_date])
        recalibrate(
            ledgers,
            {DATE: rows_today, other_date: rows_other},
            {DATE: route_b_evidence()},
            ROUTE_ACTIVE)
        self.assertEqual(len(ledgers[DATE]['revisions']), 3)
        self.assertEqual(ledgers[DATE]['current']
                         ['personal_account']['R1']['remaining_hours'], 0)
        # 未受影响日期的修订对象与内容完全不动。
        self.assertIs(ledgers[other_date]['revisions'], untouched_revisions)
        self.assertEqual(ledgers[other_date], snapshot)


if __name__ == '__main__':
    unittest.main()
