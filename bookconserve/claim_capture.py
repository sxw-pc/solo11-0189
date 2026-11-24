"""Payment-exclusive capture of grant-eligible expense lines.

The unit of funding is a *payment*, not an expense row.  Several rows may
describe the same payment (an invoice plus its journal line, for example);
they are collapsed so the payment is captured once.  A payment can be charged
to only one grant even when several grants list it as eligible.  Purpose
qualification is applied here as well, so the allocator receives a capture
view that already reflects both rules.
"""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

DEFAULT_RATE = Fraction(4, 5)


class ClaimError(ValueError):
    """Raised when expense or grant capture data is inconsistent."""


class DuplicatePayment(ClaimError):
    """Raised when one payment reference carries conflicting line content."""


def _fraction(value):
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise ClaimError('boolean is not a monetary amount')
    if isinstance(value, int):
        return Fraction(value)
    try:
        return Fraction(Decimal(str(value)))
    except (ArithmeticError, ValueError) as exc:
        raise ClaimError(f'invalid monetary amount: {value!r}') from exc


def money(value):
    """Return an int for whole amounts, otherwise a two-decimal float."""
    value = _fraction(value)
    if value.denominator == 1:
        return int(value)
    return float(round(float(value), 2))


def payment_reference(line):
    """The real payment a line belongs to; defaults to the line id."""
    return str(line.get('payment') or line['id'])


def purpose_allowed(purpose, grant):
    """Purpose qualification for one grant.

    When either side does not declare a purpose there is nothing to compare,
    so the eligibility list remains the governing rule.  When both declare a
    purpose, membership in the grant's approved purposes is required.
    """
    approved = grant.get('purposes')
    if not approved or purpose is None:
        return True
    return purpose in approved


def _resolve_grant(grant, grants):
    if isinstance(grant, dict):
        return grant
    grant_id = str(grant)
    if grants is None:
        return {'id': grant_id}
    for candidate in grants:
        if candidate['id'] == grant_id:
            return candidate
    raise ClaimError(f'unknown grant: {grant_id}')


def claimable(expenses, grant, grants=None):
    """Return lines eligible for *grant* by list membership and purpose.

    This is a single-grant view and deliberately does not decide exclusivity;
    allocation must use :func:`payments` so a shared payment is counted once.
    """
    config = _resolve_grant(grant, grants)
    grant_id = config['id']
    return [line for line in expenses
            if grant_id in line.get('eligible_grants', ())
            and purpose_allowed(line.get('purpose'), config)]


@dataclass(frozen=True)
class Payment:
    """One real payment, after duplicate rows have been reconciled."""

    reference: str
    amount: Fraction
    eligible_grants: tuple
    purpose: object
    line_ids: tuple

    def as_dict(self):
        return {'payment': self.reference,
                'amount': money(self.amount),
                'eligible_grants': list(self.eligible_grants),
                'purpose': self.purpose,
                'line_ids': list(self.line_ids)}


def _line_signature(line):
    return (str(payment_reference(line)),
            _fraction(line['amount']),
            tuple(sorted(str(g) for g in line.get('eligible_grants', ()))),
            line.get('purpose'))


def payments(expenses):
    """Group expense rows into payment-exclusive units.

    Rows that repeat the same payment reference are treated as the same
    payment.  Identical repeats are collapsed (and keep every line id);
    differing amounts, eligibility lists or purposes are a hard conflict.
    """
    groups = {}
    for line in expenses:
        if 'id' not in line:
            raise ClaimError('expense line requires an id')
        reference = payment_reference(line)
        if 'amount' not in line:
            raise ClaimError(f'payment {reference!r} requires an amount')
        amount = _fraction(line['amount'])
        if amount <= 0:
            raise ClaimError(f'payment {reference!r} amount must be positive')
        eligible = tuple(dict.fromkeys(str(g) for g in line.get('eligible_grants', ())))
        signature = _line_signature(line)
        record = groups.get(reference)
        if record is None:
            groups[reference] = {'signature': signature,
                                 'amount': amount,
                                 'eligible_grants': eligible,
                                 'purpose': line.get('purpose'),
                                 'line_ids': [line['id']]}
            continue
        if record['signature'] != signature:
            raise DuplicatePayment(
                f'payment {reference!r} has conflicting duplicate rows')
        record['line_ids'].append(line['id'])
    return {reference: Payment(reference=reference,
                               amount=record['amount'],
                               eligible_grants=record['eligible_grants'],
                               purpose=record['purpose'],
                               line_ids=tuple(record['line_ids']))
            for reference, record in groups.items()}
