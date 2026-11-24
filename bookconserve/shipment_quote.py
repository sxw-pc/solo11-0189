"""Present lowest-price shipment quote."""
from .route_capture import candidate_routes

def choose(routes, origin, destination):
    candidates = candidate_routes(routes, origin, destination)
    return min(candidates, key=lambda route: route['price'])['id']
