"""逐段温湿度采样、封箱/开箱交接与控湿箱恢复的证据采集。

本模块只做“证据投影”：把每条路线的分段采样、中转封箱事件与设备/车箱窗口
整理成带包装状态的时间线，并按采样时间累计湿度暴露。供应商自报的
``moisture_dose`` 只是摘要，绝不参与累计；中转再封箱后状态从开箱湿度重新
恢复，不沿用上一段控湿状态。
"""

# 包装状态：控湿密封 / 裸运（无控湿）/ 中转开箱暴露 / 再封箱恢复中
SEALED = 'sealed'
BARE = 'bare'
OPEN = 'open'
RECOVERY = 'recovery'


def candidate_routes(routes, origin, destination):
    """按起终点收线路（保留原签名），返回原始报价记录。"""
    return [route for route in routes
            if route['origin'] == origin and route['destination'] == destination]


def _all_samples(route, exclude_late=False):
    """返回全部采样条目。

    ``exclude_late=True`` 时排除客观迟到件（``received_hour`` 严格晚于其
    采样时点 ``at_hour``）；计划内的在途采样（二者相等）全部保留。
    """
    entries = []

    def ingest(sample, source, kind):
        received = sample.get('received_hour', sample['at_hour'])
        if exclude_late and received > sample['at_hour']:
            return
        entries.append({
            'hour': sample['at_hour'], 'sample_id': sample['id'],
            'rh': sample['rh'], 'temp_c': sample['temp_c'],
            'received_hour': received, 'source': source, 'kind': kind})

    for segment in route.get('segments', []):
        for sample in segment.get('samples', []):
            ingest(sample, segment['id'], 'segment')
    for handoff in route.get('handoffs', []):
        for sample in handoff.get('samples', []):
            ingest(sample, handoff['id'], 'handoff')
    return entries


def _samples_by_hour(route, exclude_late=False):
    """按时点归集采样；同一时点多条候选时由 build_timeline 按状态裁决。"""
    grouped = {}
    for entry in _all_samples(route, exclude_late=exclude_late):
        grouped.setdefault(entry['hour'], []).append(entry)
    return grouped


def _point_state(route, hour):
    """按包装状态判定某一时刻暴露口径，再封箱后不沿用上一段控湿状态。

    交接事件优先于运输段：再封箱后的恢复区间覆盖到下一段发车时点，
    发车时点仍按恢复中处理，箱内 RH 由实测采样给出，禁止沿用上段状态。
    """
    for handoff in route.get('handoffs', []):
        if handoff['open_hour'] <= hour < handoff['reseal_hour']:
            return OPEN
        if handoff['reseal_hour'] <= hour <= handoff.get(
                'next_depart_hour', handoff['reseal_hour']):
            return RECOVERY
    for segment in route.get('segments', []):
        if segment['depart_hour'] <= hour <= segment['arrive_hour']:
            return SEALED if segment.get('sealed') else BARE
    return None


def build_timeline(route, exclude_late=False):
    """把分段采样与封箱事件投影为带包装状态的等证据时间线。

    每个时间点必须有实测采样；状态只来自封箱/再封箱事件与分段包装标记，
    任何状态切换都在事件边界发生，禁止跨中转沿用上一段控湿状态。
    ``exclude_late=True`` 时排除客观迟到（回执晚于采样时点）的异常件。
    """
    samples = _samples_by_hour(route, exclude_late=exclude_late)
    hours = sorted(samples)
    if not hours:
        raise ValueError(f"route {route['id']} 缺少温湿度采样证据")
    timeline = []
    for hour in hours:
        state = _point_state(route, hour)
        if state is None:
            raise ValueError(
                f"route {route['id']} 采样 {hour} 时不在任何封箱/运输时段内")
        candidates = samples[hour]
        # 交接边界优先采用 handoff 采样；其次按状态优先级，再次取晚回执
        handoff = next((item for item in candidates
                        if item['kind'] == 'handoff'), None)
        if handoff is not None and state in (OPEN, RECOVERY):
            chosen = handoff
        else:
            chosen = max(candidates,
                         key=lambda item: (item['kind'] == 'handoff',
                                           item['received_hour']))
        timeline.append({'hour': hour, 'state': state, **chosen})
    return timeline


def accumulated_exposure(route, safe_rh=50, timeline=None, extra_samples=None,
                         exclude_late=False):
    """按采样时间与包装状态逐段累计湿度风险剂量。

    - 剂量只由实测 RH 对时间积分：``max(0, RH - safe_rh)``，梯形法；
    - 供应商单个 ``moisture_dose`` 摘要不参与计算；
    - ``extra_samples`` 用于迟到传感器回执补证，重算时纳入新采样点。
    """
    if timeline is None:
        timeline = build_timeline(route, exclude_late=exclude_late)
    points = {point['hour']: point['rh'] for point in timeline}
    if extra_samples:
        for sample in extra_samples:
            points[sample['at_hour']] = sample['rh']
    hours = sorted(points)
    total = 0.0
    spans = []
    for left, right in zip(hours, hours[1:]):
        rate_l = max(0.0, points[left] - safe_rh)
        rate_r = max(0.0, points[right] - safe_rh)
        dose = (right - left) * (rate_l + rate_r) / 2.0
        total += dose
        spans.append({'from_hour': left, 'to_hour': right,
                      'rh_from': points[left], 'rh_to': points[right],
                      'dose': round(dose, 6)})
    return round(total, 6), spans


def resource_occupancy(routes):
    """汇总每条路线对运输/中转车箱的占用时段（半开区间）。"""
    occupancy = {}
    for route in routes:
        for segment in route.get('segments', []):
            occupancy.setdefault(segment['resource'], []).append({
                'resource': segment['resource'], 'route': route['id'],
                'segment': segment['id'],
                'start_hour': segment['depart_hour'],
                'end_hour': segment['arrive_hour']})
    return occupancy


def find_resource_conflicts(routes, bookings=None):
    """路线占用与既有预订单（含其他卷册运输）在同一车箱上的时段重叠即冲突。

    同一路线内不同时段先后使用同一车箱属于合法接续；首尾相接
    （end==start）同样不算重复占用。
    """
    grouped = {}
    for entries in resource_occupancy(routes).values():
        for entry in entries:
            grouped.setdefault(entry['resource'], []).append(entry)
    for booking in bookings or []:
        grouped.setdefault(booking['resource'], []).append({
            'resource': booking['resource'],
            'route': booking.get('route', 'booking'),
            'segment': booking['id'],
            'start_hour': booking['start_hour'],
            'end_hour': booking['end_hour'],
            'booking': True})
    conflicts = []
    route_ids = {item['id'] for item in routes}
    for resource, entries in grouped.items():
        entries = sorted(entries, key=lambda e: (e['start_hour'], e['end_hour']))
        for earlier, later in zip(entries, entries[1:]):
            if earlier['end_hour'] <= later['start_hour']:
                continue
            external = (earlier.get('booking') or later.get('booking')
                        or earlier['route'] not in route_ids
                        or later['route'] not in route_ids)
            if external:
                conflicts.append({'resource': resource,
                                  'left': earlier, 'right': later})
    return conflicts


def late_sensor_receipts(route, issue_hour):
    """回执晚于签发时刻的采样点：签发时不可得，只能触发事后重报价。"""
    late = []
    pools = [sample for segment in route.get('segments', [])
             for sample in segment.get('samples', [])]
    pools += [sample for handoff in route.get('handoffs', [])
              for sample in handoff.get('samples', [])]
    for sample in pools:
        if sample.get('received_hour', sample['at_hour']) > issue_hour:
            late.append(sample)
    return late


def evidence_refs(route, equipment_index, window):
    """收集可核验证据：运输设备证书、签收窗口、封箱/开箱交接事件。"""
    refs = []
    if window and window.get('evidence'):
        refs.append(window['evidence'])
    for segment in route.get('segments', []):
        for key in ('resource', 'box'):
            equip = equipment_index.get(segment[key])
            if equip and equip.get('evidence'):
                refs.append(equip['evidence'])
    for handoff in route.get('handoffs', []):
        refs.extend([handoff['seal_event_open'], handoff['seal_event_reseal']])
        equip = equipment_index.get(handoff['box'])
        if equip and equip.get('evidence'):
            refs.append(equip['evidence'])
    return sorted(set(refs))
