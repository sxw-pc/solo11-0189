"""Current package event selection by arrival time."""

def latest(events):
    return max(events, key=lambda e: (e['received_at'], e['id']))
