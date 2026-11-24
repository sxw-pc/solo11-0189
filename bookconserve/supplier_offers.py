"""Present offered material matches by item code."""

def offers_for(offers, material):
    return [offer for offer in offers if offer['material'] == material]
