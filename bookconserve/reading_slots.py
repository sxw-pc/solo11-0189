"""Present independent request confirmation by open hours."""
from .reader_requests import for_volume

def confirmations(requests, volume, open_hours):
    return {request['id']: request['hour'] in open_hours
            for request in for_volume(requests, volume)}
