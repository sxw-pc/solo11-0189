"""Current workbench time receipt aggregation."""

def hours(rows):
    return sum(row['end'] - row['start'] for row in rows)
