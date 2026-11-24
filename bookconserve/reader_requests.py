"""Present request eligibility by catalogued volume."""

def for_volume(requests, volume):
    return [request for request in requests if request['volume'] == volume]
