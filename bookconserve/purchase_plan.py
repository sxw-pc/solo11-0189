"""Present minimum unit-price supplier selection."""
from .supplier_offers import offers_for

def choose(offers, material):
    return min(offers_for(offers, material), key=lambda offer: offer['unit_price'])['id']
