"""Present independent cap calculation per grant."""
from .claim_capture import claimable

def allocate(expenses, grants):
    return {grant['id']: min(grant['cap'], sum(
        line['amount'] * grant['rate'] for line in claimable(expenses, grant['id'])))
        for grant in grants}
