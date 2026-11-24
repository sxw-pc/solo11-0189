"""Present independent grant claims from eligible expense lines."""

def claimable(expenses, grant):
    return [line for line in expenses if grant in line['eligible_grants']]
