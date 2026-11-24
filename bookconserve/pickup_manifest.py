"""Present pickup approval from container weight alone."""
from .waste_capture import weight_for

def ready(entries, container, capacity):
    return weight_for(entries, container) <= capacity
