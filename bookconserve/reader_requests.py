"""读者请求、改期同意、到场签收与原件禁阅状态。

排程所需的读者侧事实全部在此显式建模，reading_slots 不得自行假设：

- ``qualified`` 缺失按未取得阅览资格处理（fail-closed）；
- 改期只能落到读者在改期回执中接受的小时，请求小时本身始终有效，
  任何未获同意的移动都属于静默互换；
- 取消、到场、禁阅都是带 id 的事件；重排只能依据事件进行，
  无预订读者即便先到场也只能记 unscheduled，不能生成签收或实物使用记录。
"""
from __future__ import annotations


def for_volume(requests, volume):
    """同一卷册的全部请求（隔离副本，供排程在卷册维度互斥）。"""
    return [request for request in requests if request['volume'] == volume]


def is_qualified(request):
    """是否已取得阅览资格；字段缺失一律按未取得处理。"""
    return bool(request.get('qualified'))


def accepted_hours(request):
    """读者可被签发的全部小时：请求小时 + 改期回执接受小时，去重排序。"""
    hours = {request['hour']}
    hours.update(request.get('accepted_hours', ()))
    return sorted(hours)


def consent_covers(request, hour):
    """该小时是否有读者同意覆盖（请求小时自带同意，其余须在回执内）。"""
    return hour == request['hour'] or hour in request.get('accepted_hours', ())


def _events(events, etype):
    return [event for event in events if event.get('type') == etype]


def cancelled_ids(events):
    return {event['request_id'] for event in _events(events, 'cancel')}


def is_cancelled(request_id, events):
    return request_id in cancelled_ids(events)


def arrivals(events):
    """request_id -> 最近一次到场事件（同 id 事件以后到者为准）。"""
    result = {}
    for event in _events(events, 'arrival'):
        result[event['request_id']] = event
    return result


def active_bans(events, volume=None):
    bans = _events(events, 'ban')
    if volume is not None:
        bans = [event for event in bans if event.get('volume') == volume]
    return bans


def is_banned(volume, events):
    return bool(active_bans(events, volume))


def arrival_hour(event):
    """ISO 时间戳折算为当天小时浮点值，如 10:45 -> 10.75。"""
    clock = event['at'].split('T', 1)[1]
    hour, minute = (int(part) for part in clock.split(':')[:2])
    return hour + minute / 60.0


def attendance(booking, events, now_hour=None):
    """读者针对已签发预订的真实签收状态。

    on_time/early/late 仅对 confirmed 预订给出；无预订读者即便先到场也只记
    unscheduled，系统绝不据此生成桌位、原件或馆员记录。
    """
    request_id = booking.get('request_id')
    event = arrivals(events).get(request_id) if request_id else None
    if booking.get('status') != 'confirmed':
        return 'unscheduled' if event else 'none'
    if event is None:
        if now_hour is not None and now_hour >= booking['hour'] + 1:
            return 'no_show'
        return 'pending'
    moment = arrival_hour(event)
    hour = booking['hour']
    if moment < hour:
        return 'early'
    if moment < hour + 1:
        return 'on_time'
    return 'late'
