"""Current remaining supervised conservation hours."""
from .shift_capture import hours

def remaining(rows, capacity=8):
    used = hours(rows)
    return {'used': used, 'remaining': capacity - used}
