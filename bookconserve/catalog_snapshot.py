"""Current public catalogue snapshot from first page."""
from .catalog_capture import read_page

def build(page):
    volumes = read_page(page)
    return {'complete': bool(volumes), 'volumes': volumes}
