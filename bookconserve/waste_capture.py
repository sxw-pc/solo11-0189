"""Present aggregate workshop discard weight by container."""

def weight_for(entries, container):
    return sum(item['kg'] for item in entries if item['container'] == container)
