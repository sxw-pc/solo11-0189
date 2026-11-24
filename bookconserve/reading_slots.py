"""阅览桌位、馆员取送与修复后避光恢复窗的确定性排程。

时间模型：小时 ``h`` 表示单元 ``[h, h+1)``。一次一小时实物翻阅 ``h`` 需要
三段互不重叠的占用：

1. ``h-1`` 馆员取件送达（delivery），原件离开修复/避光区；
2. ``h`` 原件仅占用一个阅览桌位（reading）；
3. ``h+1`` 原件归架避光恢复（recovery），馆员同时归位（collection）。

因此同一原件两次翻阅之间至少隔着一个完整的避光单元：相邻可签发起点只能是
``h`` 与 ``h+2``，中间单元合并为一次连续的馆员交接（turnover），绝不把连续
两个开放小时当成两次独立可用预约。只按开放小时去重、或把后到请求机械推后
一格，都会漏掉馆员取送与恢复窗约束。
"""
from __future__ import annotations

from itertools import product

from . import reader_requests as requests_mod
from .reader_requests import for_volume


def _open_hours(room):
    return sorted(room['open_hours'])


def _hhmm(hour):
    # 避免在同一 f-string 内把字面 ":00" 紧挨格式说明符，3.11 词法分析会误判。
    return f'{hour:02d}:00'


def _interval(hour):
    return f'{_hhmm(hour)}-{_hhmm(hour + 1)}'


def _desk_ids(room):
    return [desk['id'] for desk in room.get('desks', ())]


def fixed_librarian_tasks(room):
    """已签发、不可移动的馆员交接任务（按小时排序）。"""
    return sorted(room.get('librarian_tasks', ()), key=lambda task: task['hour'])


def eligible_requests(room, events=()):
    """同卷、有阅览资格、且未取消的活跃请求。"""
    cancelled = requests_mod.cancelled_ids(events)
    return [request for request in for_volume(room['requests'], room['volume'])
            if requests_mod.is_qualified(request)
            and request['id'] not in cancelled]


def chain_cells(hour):
    """一次翻阅占用的取送/翻阅/恢复三个单元。"""
    return (hour - 1, hour, hour + 1)


def generated_task_cells(bookings):
    """由已签发翻阅推导馆员与原件占用单元。

    起点间隔为 2 的两次翻阅共享中间单元：前一次的归位与后一次的取件合并成
    一次 turnover（馆员一次连续取送），不得拆成两条互斥任务。
    """
    hours = sorted(booking['hour'] for booking in bookings)
    cells = {}
    for index, hour in enumerate(hours):
        cells.setdefault(hour - 1, {'hour': hour - 1, 'kind': 'delivery'})
        cells[hour] = {'hour': hour, 'kind': 'reading'}
        next_hour = hours[index + 1] if index + 1 < len(hours) else None
        if next_hour is not None and next_hour - hour == 2:
            cells[hour + 1] = {'hour': hour + 1, 'kind': 'turnover'}
        else:
            cells[hour + 1] = {'hour': hour + 1, 'kind': 'collection'}
    return [cells[key] for key in sorted(cells)]


def check_assignment(room, assignment):
    """校验 {request_id: hour} 是否构成可执行排程，返回不可行原因列表。

    空列表即可行。检查覆盖：读者同意、开放小时、原件互斥与避光间隔、
    馆员取送窗（含不可移动任务及工时）、桌位容量。
    """
    reasons = []
    requests_by_id = {request['id']: request for request in room['requests']}
    hours = []
    for request_id, hour in sorted(assignment.items()):
        request = requests_by_id[request_id]
        if not requests_mod.consent_covers(request, hour):
            reasons.append({'request_id': request_id, 'hour': hour,
                            'reason': '缺少该读者的改期同意，禁止静默互换'})
        if hour not in _open_hours(room):
            reasons.append({'request_id': request_id, 'hour': hour,
                            'reason': '不在当天开放小时内'})
        hours.append(hour)

    hours = sorted(hours)
    for earlier, later in zip(hours, hours[1:]):
        if later - earlier < 2:
            reasons.append({'hour': later,
                            'reason': '同一原件两次翻阅之间缺少完整避光恢复单元'})

    staff_hours = set(room.get('staff_hours', ()))
    fixed = {task['hour']: task for task in fixed_librarian_tasks(room)}
    for cell in generated_task_cells(
            [{'hour': hour} for hour in hours]):
        if cell['hour'] in fixed:
            reasons.append({'hour': cell['hour'],
                            'reason': f"馆员单元{cell['hour']:02d}时与已签发不可移动任务"
                                      f"「{fixed[cell['hour']].get('name', '')}」冲突"})
        if staff_hours and cell['hour'] not in staff_hours:
            reasons.append({'hour': cell['hour'],
                            'reason': f"馆员单元{cell['hour']:02d}时不在馆员工时内"})

    # 逐小时清点：同一小时的翻阅数既受桌位数约束，同卷时更只能为 1。
    desks = _desk_ids(room)
    per_hour = {}
    for hour in hours:
        per_hour[hour] = per_hour.get(hour, 0) + 1
    for hour, count in sorted(per_hour.items()):
        if count > 1:
            reasons.append({'hour': hour,
                            'reason': '一册原件同一时刻只能在一个阅览桌位'})
        if desks and count > len(desks):
            reasons.append({'hour': hour,
                            'reason': '同一小时阅览桌位不足'})
    return reasons


def _candidate_choices(room, events=()):
    choices = []
    for request in eligible_requests(room, events):
        options = [None] + [hour for hour in requests_mod.accepted_hours(request)
                            if hour in _open_hours(room)]
        choices.append((request['id'], options))
    return choices


def legal_schedules(room, events=()):
    """枚举全部最大可执行排程，按 (request_id, hour) 排序后返回。

    每个元素是 {request_id: hour}；未出现的请求即无位可排、只能候补。
    只保留签发人数最多的排程，便于区分“两种合法排程”与部分退化方案。
    """
    choices = _candidate_choices(room, events)
    feasible = []
    for combo in product(*(options for _, options in choices)):
        assignment = {request_id: hour
                      for (request_id, _), hour in zip(choices, combo)
                      if hour is not None}
        if assignment and not check_assignment(room, assignment):
            feasible.append(assignment)
    if not feasible:
        return []
    maximum = max(len(assignment) for assignment in feasible)
    feasible = [assignment for assignment in feasible
                if len(assignment) == maximum]
    return sorted(feasible,
                  key=lambda assignment: tuple(sorted(assignment.items())))


def _desk_plan(room, bookings):
    desks = _desk_ids(room)
    occupancy, assignment = [], {}
    if not desks:
        return occupancy, assignment
    confirmed = [booking for booking in bookings
                 if booking['status'] == 'confirmed']
    for index, booking in enumerate(
            sorted(confirmed, key=lambda item: (item['hour'],
                                                item['request_id']))):
        desk = desks[index % len(desks)]
        assignment[booking['request_id']] = desk
        occupancy.append({'hour': booking['hour'], 'desk': desk,
                          'volume': room['volume'],
                          'request_id': booking['request_id']})
    return sorted(occupancy, key=lambda item: (item['hour'], item['desk'])), assignment


def _notice(room, request, hour):
    if hour == request['hour']:
        return (f"{request['id']}：{room['volume']} 原件已签发 "
                f"{_interval(hour)} 阅览，请按时到场签收。")
    return (f"{request['id']}：您请求的 {request['hour']:02d} 时与同卷请求冲突，"
            f"依您的改期回执改签发 {_interval(hour)}，"
            f"原 {request['hour']:02d} 时承诺不再保留。")


def _assemble(room, assignment, events=()):
    requests_by_id = {request['id']: request for request in room['requests']}
    bookings, notices = [], []
    for request_id, hour in sorted(assignment.items(),
                                   key=lambda item: (item[1], item[0])):
        request = requests_by_id[request_id]
        bookings.append({'request_id': request_id, 'hour': hour,
                         'requested_hour': request['hour'],
                         'status': 'confirmed',
                         'rescheduled': hour != request['hour'],
                         'attendance': requests_mod.attendance(
                             {'status': 'confirmed', 'hour': hour}, events)})
        notices.append({'request_id': request_id, 'kind': 'confirmed',
                        'hour': hour, 'text': _notice(room, request, hour)})
    assigned = set(assignment)
    for request in room['requests']:
        if request['id'] in assigned:
            continue
        if not requests_mod.is_qualified(request):
            continue
        # 取消与候补都要保留请求条目：取消须留原承诺痕迹，且同样不得代记到场。
        bookings.append({'request_id': request['id'], 'hour': None,
                         'requested_hour': request['hour'],
                         'status': 'waitlisted', 'rescheduled': False,
                         'attendance': requests_mod.attendance(
                             {'status': 'waitlisted',
                              'request_id': request['id']}, events)})
        if requests_mod.is_cancelled(request['id'], events):
            continue
        notices.append({'request_id': request['id'], 'kind': 'waitlisted',
                        'hour': None,
                        'text': f"{request['id']}：{room['volume']} 原件当天无合规"
                                f"空窗，已转候补；不会代记到场。"})

    occupancy, desk_assignment = _desk_plan(room, bookings)
    confirmed = [booking for booking in bookings
                 if booking['status'] == 'confirmed']
    task_cells = generated_task_cells(confirmed)
    fixed_ids = {task['hour']: task['id']
                 for task in fixed_librarian_tasks(room)}
    usage = []
    for cell in task_cells:
        entry = {'hour': cell['hour'], 'volume': room['volume'],
                 'kind': cell['kind']}
        if cell['kind'] == 'reading':
            booking = next(item for item in confirmed
                           if item['hour'] == cell['hour'])
            entry.update({'request_id': booking['request_id'],
                          'desk': desk_assignment[booking['request_id']],
                          'attendance': booking['attendance']})
        else:
            entry['desk'] = None
            if cell['hour'] in fixed_ids:
                entry['librarian_task'] = fixed_ids[cell['hour']]
        usage.append(entry)
    for task in fixed_librarian_tasks(room):
        if not any(entry['hour'] == task['hour'] for entry in usage):
            usage.append({'hour': task['hour'], 'volume': room['volume'],
                          'kind': 'external_handover', 'desk': None,
                          'librarian_task': task['id']})
    recovery = [entry['hour'] for entry in usage
                if entry['kind'] in ('collection', 'turnover')]
    return {'volume': room['volume'], 'bookings': bookings,
            'notices': notices, 'usage': sorted(usage, key=lambda entry: entry['hour']),
            'desk_occupancy': occupancy, 'recovery_hours': recovery,
            'librarian_cells': [entry['hour'] for entry in usage
                                if entry['kind'] != 'reading']}


def issue_schedule(room, assignment, events=()):
    """对单个 {request_id: hour} 方案签发预约/候补通知与当天实物使用记录。"""
    reasons = check_assignment(room, assignment)
    if reasons:
        raise ValueError(f'排程不可执行: {reasons}')
    return _assemble(room, assignment, events)


def confirmations(room, plan=None, events=()):
    """逐位读者是否在其请求小时获得确认。

    修复旧实现“只看 hour 是否在开放小时”的双确认错误：同一卷册同一时刻只能
    有一位读者拿到原件，被改期或候补的请求在请求小时上一律为 False。
    """
    if plan is None:
        plan = _assemble(room, {}, events)
    result = {}
    for request in for_volume(room['requests'], room['volume']):
        booking = next((item for item in plan['bookings']
                        if item['request_id'] == request['id']), None)
        result[request['id']] = bool(
            booking and booking['status'] == 'confirmed'
            and booking['hour'] == request['hour'])
    return result


def _preference_distance(room, assignment):
    requests_by_id = {request['id']: request for request in room['requests']}
    return sum(abs(hour - requests_by_id[request_id]['hour'])
               for request_id, hour in assignment.items())


def _best_extension(room, pinned, free, events):
    """在固定承诺之外，为未开始的候补请求求最大可执行扩展。

    优先级：确认人数最多 → 离各自请求小时的总偏移最小（少折腾读者）→
    字典序兜底，保证离线确定性。
    """
    best, best_score = None, None
    choices = []
    for request in free:
        options = [None] + [hour for hour
                            in requests_mod.accepted_hours(request)
                            if hour in _open_hours(room)]
        choices.append((request['id'], options))
    for combo in product(*(options for _, options in choices)):
        candidate = dict(pinned)
        candidate.update({request_id: hour
                          for (request_id, _), hour in zip(choices, combo)
                          if hour is not None})
        if check_assignment(room, candidate):
            continue
        score = (len(candidate),
                 -_preference_distance(room, candidate),
                 tuple(sorted(candidate.items())))
        if best_score is None or score > best_score:
            best, best_score = candidate, score
    return best or {}


def replan(room, prior_plan, events, now_hour=0):
    """依据取消/迟到/禁阅事件重排当天日程。

    硬规则：已开始或已签收时段原样保留；取消只释放未开始时段；迟到不移动
    任何已通知承诺；禁阅下发后相关预订全部转候补，但无事件不得伪造到场。
    """
    volume = room['volume']
    prior_by_id = {booking['request_id']: booking
                   for booking in prior_plan['bookings']}
    arrival_map = requests_mod.arrivals(events)

    if requests_mod.is_banned(volume, events):
        bookings, notices = [], []
        for booking in prior_plan['bookings']:
            item = dict(booking)
            if item['status'] == 'confirmed':
                item['status'] = 'waitlisted'
                item['hour'] = None
                item['attendance'] = requests_mod.attendance(
                    {'status': 'waitlisted'}, events)
                notices.append({
                    'request_id': item['request_id'], 'kind': 'banned',
                    'hour': None,
                    'text': f"{item['request_id']}：修复复检下发禁阅，原 "
                            f"{booking['hour']:02d} 时阅览预订全部转候补，"
                            f"原件停止取送，不代记到场。"})
            bookings.append(item)
        bans = requests_mod.active_bans(events, volume)
        plan = {'volume': volume, 'bookings': bookings, 'notices': notices,
                'usage': sorted((dict(entry, suspended=True, attendance='none')
                                 for entry in prior_plan['usage']
                                 if entry['kind'] == 'reading'),
                                key=lambda entry: entry['hour']),
                'desk_occupancy': [], 'recovery_hours': [],
                'librarian_cells': [],
                'ban': {'reason': bans[-1].get('reason', '修复复检禁阅'),
                        'event_id': bans[-1]['id']}}
        return plan

    cancelled = requests_mod.cancelled_ids(events)
    pinned = {}
    historical = []
    for booking in prior_plan['bookings']:
        if booking['status'] != 'confirmed':
            continue
        request_id, hour = booking['request_id'], booking['hour']
        if request_id in cancelled:
            # 取消仅释放“未开始”承诺；已开始/已签收时段不可取消、原样保留。
            if hour <= now_hour or request_id in arrival_map:
                historical.append(booking)
                pinned[request_id] = hour
            continue
        # 已告知时段不得静默互换：已开始、已到场或未开始但仍有效的承诺全部固定。
        historical.append(booking)
        pinned[request_id] = hour

    free = [request for request in eligible_requests(room, events)
            if request['id'] not in pinned
            and request['id'] not in cancelled]
    assignment = _best_extension(room, pinned, free, events)
    # 扩展可能未加入任何自由请求，仍须把全部固定承诺带回签发结果。
    for request_id, hour in pinned.items():
        assignment.setdefault(request_id, hour)
    plan = _assemble(room, assignment, events)

    historical_ids = {booking['request_id'] for booking in historical}
    for booking in plan['bookings']:
        old = prior_by_id.get(booking['request_id'])
        booking['changed'] = not old or (
            old['status'], old['hour']) != (booking['status'], booking['hour'])
        booking['pinned'] = booking['request_id'] in historical_ids
        if old and old['status'] == 'confirmed' \
                and booking['request_id'] in cancelled:
            booking['cancelled_hour'] = old['hour']
            plan['notices'].append({
                'request_id': booking['request_id'], 'kind': 'cancelled',
                'hour': old['hour'],
                'text': f"{booking['request_id']}：原 {old['hour']:02d} 时承诺已按"
                        f"取消事件留痕，释放的未开始时段方可再排。"})
    plan['pinned_hours'] = sorted(
        booking['hour'] for booking in historical)
    return plan
