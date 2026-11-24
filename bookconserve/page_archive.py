"""Current page completeness based on scan upload count."""
from .tile_capture import uploads

def assess(rows, expected=3):
    count = uploads(rows)
    return {'uploads': count, 'status': '完整' if count >= expected else '缺图'}
