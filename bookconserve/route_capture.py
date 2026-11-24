"""Present route intake by origin and destination."""

def candidate_routes(routes, origin, destination):
    return [route for route in routes if route['origin'] == origin
            and route['destination'] == destination]
