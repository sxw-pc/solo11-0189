"""时变湿度暴露驱动的运输规划与可执行运输单签发。

选择逻辑不再读取供应商自报的 ``moisture_dose`` 字段，也不按最低价排序：
每条候选路线先由 :mod:`bookconserve.route_capture` 的逐段采样、封箱/开箱
事件与控湿恢复时间重算实际风险剂量，再核对 40 剂量上限、17 时到馆窗口、
车箱占用不重复及设备/签收窗口证据，全部通过才可签发。
"""
import copy
import hashlib
import json

from .route_capture import (
    accumulated_exposure,
    build_timeline,
    candidate_routes,
    evidence_refs,
    find_resource_conflicts,
)

DOSE_LIMIT = 40
ARRIVAL_DEADLINE = 17


def _equipment_index(ledger):
    return {item['id']: item for item in ledger.get('equipment', [])}


def evaluate_route(route, ledger, include_late=False):
    """用实测采样重算一条路线，返回带可核验明细的评估结果。

    默认排除客观迟到回执（``received_hour`` 晚于采样时点）；``include_late``
    为真时全量纳入，用于事后重算。
    """
    safe_rh = ledger.get('safe_rh', 50)
    limit = ledger.get('dose_limit', DOSE_LIMIT)
    window = ledger.get('receiving_window', {})
    deadline = window.get('close_hour', ARRIVAL_DEADLINE)
    timeline = build_timeline(route, exclude_late=not include_late)
    dose, spans = accumulated_exposure(route, safe_rh=safe_rh, timeline=timeline)

    evidence = evidence_refs(route, _equipment_index(ledger), window)
    checks = {
        'exposure_under_limit': dose <= limit,
        'arrival_in_window': route['arrival_hour'] <= deadline and
        route['arrival_hour'] >= window.get('open_hour', 0),
        'no_resource_double_booking': not find_resource_conflicts(
            ledger['routes'], ledger.get('bookings', [])),
        'evidence_complete': bool(evidence),
    }
    # 供应商摘要与实测并列展示，仅用于揭示差异，不参与任何判定
    supplier = route.get('moisture_dose')
    return {
        'id': route['id'],
        'feasible': all(checks.values()),
        'checks': checks,
        'actual_exposure': dose,
        'supplier_declared_dose': supplier,
        'dose_delta_vs_supplier': None if supplier is None
        else round(dose - supplier, 6),
        'arrival_hour': route['arrival_hour'],
        'price': route['price'],
        'timeline': timeline,
        'exposure_spans': spans,
        'evidence': evidence,
        'reasons': [name for name, ok in checks.items() if not ok],
    }


def executable_plans(ledger, include_late=False):
    """评估全部起终点匹配的路线，区分可签发计划与拒签路线。

    默认排除客观迟到回执；它们只能在 :func:`requote_for_late_receipt`
    的事后重算中补入。``include_late=True`` 时全量纳入。
    """
    routes = candidate_routes(ledger['routes'], ledger['origin'],
                              ledger['destination'])
    evaluated = [evaluate_route(route, ledger, include_late=include_late)
                 for route in routes]
    feasible = [item for item in evaluated if item['feasible']]
    rejected = [item for item in evaluated if not item['feasible']]
    for plan in feasible:
        plan['tradeoff'] = _tradeoff(plan, feasible)
    return {
        'volume': ledger['volume'],
        'dose_limit': ledger.get('dose_limit', DOSE_LIMIT),
        'arrival_deadline': ledger.get('receiving_window', {})
        .get('close_hour', ARRIVAL_DEADLINE),
        'feasible': feasible,
        'rejected': rejected,
    }


def _tradeoff(plan, feasible):
    earliest = min(item['arrival_hour'] for item in feasible)
    cheapest = min(item['price'] for item in feasible)
    notes = []
    if plan['arrival_hour'] == earliest:
        notes.append('最早到馆')
    if plan['price'] == cheapest:
        notes.append('最低价')
    if plan['arrival_hour'] > earliest:
        earliest_price = min(item['price'] for item in feasible
                             if item['arrival_hour'] == earliest)
        saving = earliest_price - plan['price']
        delay = plan['arrival_hour'] - earliest
        notes.append(f'较最早方案晚到 {delay} 小时，节省 {saving} 元')
    return '、'.join(notes)


def choose(ledger_or_routes, origin=None, destination=None):
    """返回经实测核对的可执行计划（不再返回最低价路线 id）。

    兼容旧式调用 ``choose(routes, origin, destination)``：自动组装带默认
    剂量上限、17 时窗口与空预订单的最小账本。
    """
    if origin is not None:
        equipment_ids = {ref for item in ledger_or_routes
                         for segment in item.get('segments', [])
                         for ref in (segment.get('resource'),
                                     segment.get('box')) if ref}
        ledger = {
            'routes': ledger_or_routes, 'origin': origin,
            'destination': destination, 'volume': 'unknown',
            'dose_limit': DOSE_LIMIT, 'safe_rh': 50,
            'receiving_window': {'open_hour': 0,
                                 'close_hour': ARRIVAL_DEADLINE,
                                 'evidence': 'window-default'},
            'bookings': [],
            'equipment': [{'id': eq_id, 'evidence': f'eq-{eq_id}'}
                          for eq_id in sorted(equipment_ids)]}
    else:
        ledger = ledger_or_routes
    plans = executable_plans(ledger)
    if not plans['feasible']:
        raise ValueError('没有同时满足剂量上限与到馆窗口的可执行路线')
    return plans


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def issue_order(ledger, route_id, issue_hour, issued_by='Museum-B'):
    """签发可执行运输单：冻结实测剂量、到馆时刻、价格与证据，内容不可变。"""
    plans = executable_plans(ledger, include_late=False)
    selected = next((item for item in plans['feasible']
                     if item['id'] == route_id), None)
    if selected is None:
        rejected = next((item for item in plans['rejected']
                         if item['id'] == route_id), None)
        raise ValueError(
            f"路线 {route_id} 不可签发："
            f"{rejected['checks'] if rejected else '不在候选范围'}")
    route = next(item for item in ledger['routes'] if item['id'] == route_id)
    equipment = _equipment_index(ledger)
    order = {
        'order_id': f"SHIP-{ledger['volume']}-{route_id}",
        'volume': ledger['volume'],
        'route_id': route_id,
        'origin': route['origin'],
        'destination': route['destination'],
        'issued_hour': issue_hour,
        'issued_by': issued_by,
        'vehicles_and_transfers': [
            {'segment': segment['id'], 'resource': segment['resource'],
             'resource_evidence': equipment.get(
                 segment['resource'], {}).get('evidence'),
             'box': segment['box'],
             'box_evidence': equipment.get(segment['box'], {}).get('evidence'),
             'sealed': segment['sealed'],
             'depart_hour': segment['depart_hour'],
             'arrive_hour': segment['arrive_hour']}
            for segment in route['segments']],
        'handoffs': [
            {'handoff': handoff['id'], 'at': handoff['at'],
             'open_hour': handoff['open_hour'],
             'reseal_hour': handoff['reseal_hour'],
             'next_depart_hour': handoff.get('next_depart_hour'),
             'recovery_hours': equipment.get(
                 handoff['box'], {}).get('recovery_hours'),
             'seal_open_event': handoff['seal_event_open'],
             'seal_reseal_event': handoff['seal_event_reseal']}
            for handoff in route.get('handoffs', [])],
        'arrival_hour': selected['arrival_hour'],
        'actual_exposure': selected['actual_exposure'],
        'dose_limit': plans['dose_limit'],
        'price': selected['price'],
        'exposure_spans': selected['exposure_spans'],
        'evidence': selected['evidence'],
        'receiving_window': ledger['receiving_window'],
        'superseded_by': None,
        'status': 'issued',
    }
    order['fingerprint'] = hashlib.sha256(
        _canonical({k: v for k, v in order.items()
                    if k != 'fingerprint'}).encode()).hexdigest()
    return order


def _replacement(ledger, exclude_route):
    """为受影响卷册重报价：在仍可行的其他路线里选最低价方案。"""
    plans = executable_plans(ledger)
    candidates = [item for item in plans['feasible']
                  if item['id'] != exclude_route]
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item['price'], item['arrival_hour']))


def requote_for_late_receipt(ledger, order, late_samples):
    """迟到传感器回执：原版运输单保留，按新采样重算并仅对本卷重报价。"""
    route = next(item for item in ledger['routes']
                 if item['id'] == order['route_id'])
    safe_rh = ledger.get('safe_rh', 50)
    # 重算视角纳入全部已回执采样（含迟到件），调用方补证通过 extra_samples 合并
    timeline = build_timeline(route)
    dose, spans = accumulated_exposure(
        route, safe_rh=safe_rh, timeline=timeline,
        extra_samples=late_samples)
    limit = ledger.get('dose_limit', DOSE_LIMIT)
    affected_hours = sorted(sample['at_hour'] for sample in late_samples)
    liability = {'from_hour': min(affected_hours),
                 'to_hour': max(affected_hours),
                 'basis': '迟到传感器回执区间'}
    original = copy.deepcopy(order)
    original['status'] = 'retained-under-appeal'
    result = {
        'original_order': original,
        'volume': order['volume'],
        'recomputed_exposure': dose,
        'dose_limit': limit,
        'still_within_limit': dose <= limit,
        'liability_window': liability,
        'recomputed_spans': spans,
        'other_volumes_affected': False,
    }
    if dose > limit:
        replacement = _replacement(ledger, order['route_id'])
        result['requote'] = None if replacement is None else {
            'route_id': replacement['id'],
            'price': replacement['price'],
            'arrival_hour': replacement['arrival_hour'],
            'actual_exposure': replacement['actual_exposure']}
    return result


def requote_for_broken_seal(ledger, order, breach_event):
    """封箱破损：原版运输单保留，责任区间覆盖破损至再封箱，仅对本卷重报价。"""
    route = next(item for item in ledger['routes']
                 if item['id'] == order['route_id'])
    safe_rh = ledger.get('safe_rh', 50)
    # 破损后箱内控湿失效，按破损环境 RH 重算，恢复须从再封箱重新开始
    timeline = build_timeline(route)
    dose, spans = accumulated_exposure(
        route, safe_rh=safe_rh, timeline=timeline,
        extra_samples=breach_event.get('samples', []))
    limit = ledger.get('dose_limit', DOSE_LIMIT)
    liability = {
        'from_hour': breach_event['detected_hour'],
        'to_hour': breach_event['reseal_hour'],
        'basis': '封箱破损至再封箱',
        'seal_event': breach_event.get('seal_event_id')}
    original = copy.deepcopy(order)
    original['status'] = 'retained-under-appeal'
    replacement = _replacement(ledger, order['route_id'])
    return {
        'original_order': original,
        'volume': order['volume'],
        'recomputed_exposure': dose,
        'dose_limit': limit,
        'still_within_limit': dose <= limit,
        'liability_window': liability,
        'recomputed_spans': spans,
        'other_volumes_affected': False,
        'requote': None if replacement is None else {
            'route_id': replacement['id'],
            'price': replacement['price'],
            'arrival_hour': replacement['arrival_hour'],
            'actual_exposure': replacement['actual_exposure']},
    }
