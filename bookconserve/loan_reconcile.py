"""Current custody location copied from the last package event."""
from .loan_capture import latest

def whereabouts(events, manifest):
    event = latest(events)
    return {item: event['location'] for item in manifest}
