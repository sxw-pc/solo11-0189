"""Global, payment-exclusive allocation of shared expenses across grants.

Rules enforced here, not by ordering grants or expenses:

* A payment can be charged to only one grant, even when several grants list
  it as eligible.  The payment-exclusive units come from
  :func:`bookconserve.claim_capture.payments`.
* A grant can pay at most ``cap`` money and at most ``rate`` (default 80%) of
  each payment's actual amount.
* The part a grant does not pay is museum matching money.  It must exist as
  unoccupied own funds; a share is never treated as "free".
* A signed (already submitted) claim batch is locked.  Re-running allocation
  reproduces the signed shares exactly and never moves a payment to another
  grant.  If current data cannot honour a signed batch, a
  :class:`SignedBatchConflict` says so; the batch must be reversed with
  :func:`reverse_batch` and refiled as a new batch, which preserves the
  original signed digest.

The search maximises total claimed money, then the number of funded payments.
Only global optima are returned; ties are alternatives that finance may choose
between (for example the two routes in the seed fixture).  A caller can force
a route with ``preferred={payment: grant}``; the route must itself be a global
optimum, so a request that would waste claimable capacity is refused rather
than silently followed.
"""
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction

from .claim_capture import (
    DEFAULT_RATE,
    ClaimError,
    _fraction,
    money,
    payments,
    purpose_allowed,
)

__all__ = [
    'Allocation', 'Line', 'SignedBatch', 'ClaimError', 'InvalidGrant',
    'NoFeasibleRoute', 'SignedBatchConflict', 'allocate', 'allocate_fixture',
    'reverse_batch', 'scenarios', 'sign_claims', 'signed_batch',
    'verify_signed',
]


class InvalidGrant(ClaimError):
    """Raised for a malformed grant definition."""


class NoFeasibleRoute(ClaimError):
    """Raised when a requested preference cannot be met by any legal route."""


class SignedBatchConflict(ClaimError):
    """Raised when current data cannot honour an already signed claim batch."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _floor(value):
    value = _fraction(value)
    return value.numerator // value.denominator


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _grant_config(grants):
    result = {}
    for grant in grants:
        if 'id' not in grant:
            raise InvalidGrant('grant requires an id')
        grant_id = str(grant['id'])
        if grant_id in result:
            raise InvalidGrant(f'duplicate grant id: {grant_id}')
        if 'cap' not in grant:
            raise InvalidGrant(f'grant {grant_id} requires a cap')
        cap = _fraction(grant['cap'])
        if cap < 0:
            raise InvalidGrant(f'grant {grant_id} cap must not be negative')
        rate = _fraction(grant.get('rate', DEFAULT_RATE))
        if not 0 < rate <= 1:
            raise InvalidGrant(f'grant {grant_id} rate must be in (0, 1]')
        purposes = tuple(grant.get('purposes') or ())
        result[grant_id] = {'id': grant_id, 'cap': cap, 'rate': rate,
                            'purposes': purposes}
    if not result:
        raise InvalidGrant('at least one grant is required')
    return result


@dataclass(frozen=True)
class Line:
    """One funded payment: who pays, how much, and the matching money."""

    payment: str
    purpose: object
    amount: Fraction
    grant: object
    claim: Fraction
    own: Fraction
    line_ids: tuple
    locked: bool = False
    batch: object = None

    def as_dict(self):
        return {'payment': self.payment, 'purpose': self.purpose,
                'amount': money(self.amount), 'grant': self.grant,
                'claim': money(self.claim), 'own': money(self.own),
                'line_ids': list(self.line_ids), 'locked': self.locked,
                'batch': self.batch}


def _batch_payload(batch_id, grant, issued_at, lines):
    return {'batch_id': batch_id, 'grant': grant, 'issued_at': issued_at,
            'lines': [{'payment': payment, 'amount': money(amount),
                       'claim': money(claim), 'own': money(own)}
                      for payment, amount, claim, own in lines]}


def _batch_digest(batch_id, grant, issued_at, lines):
    return _digest(_batch_payload(batch_id, grant, issued_at, lines))


@dataclass(frozen=True)
class SignedBatch:
    """An immutable, already submitted claim for one grant.

    ``lines`` holds ``(payment, amount, claim, own)`` tuples.  ``digest`` is
    the SHA-256 of the canonical payload; changing a line without re-signing
    is detected and rejected.
    """

    batch_id: str
    grant: str
    issued_at: str
    lines: tuple
    digest: str

    def payload(self):
        return _batch_payload(self.batch_id, self.grant, self.issued_at,
                              self.lines)

    def as_dict(self):
        return {**self.payload(), 'digest': self.digest}


def signed_batch(batch_id, grant, lines, issued_at=None):
    """Build a signed batch from plain line dicts.

    Each line needs ``payment``, ``amount`` and ``claim``; ``own`` is derived
    when omitted.  The digest is computed here, so callers can hand the batch
    to :func:`allocate` as a lock.
    """
    issued_at = issued_at or _now()
    normalised = []
    for line in lines:
        amount = _fraction(line['amount'])
        claim = _fraction(line['claim'])
        own = _fraction(line['own']) if 'own' in line else amount - claim
        normalised.append((str(line['payment']), amount, claim, own))
    return SignedBatch(batch_id=str(batch_id), grant=str(grant),
                       issued_at=issued_at, lines=tuple(normalised),
                       digest=_batch_digest(str(batch_id), str(grant),
                                            issued_at, tuple(normalised)))


def verify_signed(batch):
    """Verify a batch's digest and basic structure; raise on tampering."""
    if not isinstance(batch, SignedBatch):
        raise SignedBatchConflict('signed batch must be a SignedBatch')
    if not batch.lines:
        raise SignedBatchConflict(f'signed batch {batch.batch_id} has no lines')
    expected = _batch_digest(batch.batch_id, batch.grant, batch.issued_at,
                             batch.lines)
    if expected != batch.digest:
        raise SignedBatchConflict(
            f'signed batch {batch.batch_id} digest mismatch; '
            'the signed content was altered and must be reversed and refiled')
    seen = set()
    for payment, amount, claim, own in batch.lines:
        if payment in seen:
            raise SignedBatchConflict(
                f'signed batch {batch.batch_id} repeats payment {payment!r}')
        seen.add(payment)
        if amount <= 0 or claim <= 0 or claim > amount or own != amount - claim:
            raise SignedBatchConflict(
                f'signed batch {batch.batch_id} has an invalid line for {payment!r}')
    return True


def reverse_batch(batch, reason, issued_at=None, batch_id=None):
    """Return a reversal record that keeps the original signed digest.

    The reversal does not erase the original claim.  Exclude the original
    batch from the next :func:`allocate` call, then sign fresh batches for the
    new route; the reversal record is the audit trail tying the two together.
    """
    verify_signed(batch)
    if not reason:
        raise ClaimError('a reversal reason is required')
    issued_at = issued_at or _now()
    payload = {'record': 'grant-claim-reversal',
               'batch_id': batch_id or f'{batch.batch_id}-REV',
               'grant': batch.grant,
               'issued_at': issued_at,
               'reverses': batch.digest,
               'reason': reason,
               'original': batch.payload()}
    return {**payload, 'digest': _digest(payload)}


def _reserve_locks(signed, payment_map, grant_map):
    """Validate signed batches and reserve their caps and matching money."""
    locks = {}
    reserved_caps = {grant_id: config['cap']
                     for grant_id, config in grant_map.items()}
    used = Fraction(0)
    if not signed:
        return locks, reserved_caps, used
    for batch in signed:
        verify_signed(batch)
        if batch.grant not in grant_map:
            raise SignedBatchConflict(
                f'signed batch {batch.batch_id} names unknown grant {batch.grant!r}')
        for payment_ref, amount, claim, own in batch.lines:
            payment = payment_map.get(payment_ref)
            if payment is None:
                raise SignedBatchConflict(
                    f'signed batch {batch.batch_id} names unknown payment '
                    f'{payment_ref!r}; it must be reversed and refiled')
            if payment_ref in locks:
                raise SignedBatchConflict(
                    f'payment {payment_ref!r} appears in more than one signed batch; '
                    'reverse one of them before allocating')
            config = grant_map[batch.grant]
            if amount != payment.amount:
                raise SignedBatchConflict(
                    f'payment {payment_ref!r} amount changed since batch '
                    f'{batch.batch_id} was signed; reverse it and refile')
            if (batch.grant not in payment.eligible_grants
                    or not purpose_allowed(payment.purpose, config)):
                raise SignedBatchConflict(
                    f'payment {payment_ref!r} is no longer eligible for grant '
                    f'{batch.grant} under signed batch {batch.batch_id}; '
                    'reverse it and refile')
            limit = min(reserved_caps[batch.grant],
                        Fraction(_floor(config['rate'] * payment.amount)))
            if claim > limit:
                raise SignedBatchConflict(
                    f'signed batch {batch.batch_id} claims {money(claim)} for '
                    f'{payment_ref!r} but only {money(limit)} is allowed now; '
                    'reverse it and refile a new batch')
            reserved_caps[batch.grant] -= claim
            used += own
            locks[payment_ref] = (batch.grant, claim, own, batch.batch_id)
    return locks, reserved_caps, used


class Allocation(Mapping):
    """A complete allocation, usable as ``{grant: claimed_amount}``."""

    def __init__(self, lines, grant_map, pool_total, signed, unpairable,
                 currency='CNY'):
        self.lines = tuple(lines)
        self.currency = currency
        self.signed_batches = tuple(signed)
        self.unpairable = tuple(unpairable)
        self._pool_total = _fraction(pool_total)
        self._grant_caps = {grant_id: config['cap']
                            for grant_id, config in grant_map.items()}
        claimed = {grant_id: Fraction(0) for grant_id in grant_map}
        for line in self.lines:
            claimed[line.grant] += line.claim
        self.grants = {}
        for grant_id, config in grant_map.items():
            self.grants[grant_id] = {
                'cap': money(config['cap']),
                'rate': float(config['rate']),
                'purposes': list(config['purposes']),
                'claimed': money(claimed[grant_id]),
                'remaining': money(config['cap'] - claimed[grant_id]),
            }
        if any(claimed[g] > config['cap'] for g, config in grant_map.items()):
            raise ClaimError('grant cap exceeded')
        self.total_expense = money(sum((line.amount for line in self.lines), Fraction(0)))
        self.total_claimed = money(sum((line.claim for line in self.lines), Fraction(0)))
        self.total_own = money(sum((line.own for line in self.lines), Fraction(0)))
        self.self_fund = {'total': money(self._pool_total),
                          'used': self.total_own,
                          'remaining': money(self._pool_total
                                             - sum((line.own for line in self.lines),
                                                   Fraction(0)))}
        self._validate()

    def _validate(self):
        by_payment = {}
        for line in self.lines:
            if line.grant not in self.grants:
                raise ClaimError(f'line for unknown grant {line.grant!r}')
            if by_payment.get(line.payment, line.grant) != line.grant:
                raise ClaimError(f'payment {line.payment!r} assigned to two grants')
            by_payment[line.payment] = line.grant
            if line.claim < 0 or line.own < 0 or line.claim > line.amount:
                raise ClaimError(f'payment {line.payment!r} shares are invalid')
            if line.claim + line.own != line.amount:
                raise ClaimError(f'payment {line.payment!r} shares do not add up')
        self._validate_totals()

    def _validate_totals(self):
        own = sum((line.own for line in self.lines), Fraction(0))
        if own > self._pool_total:
            raise ClaimError(
                f'matching funds of {money(self._pool_total)} cannot cover '
                f'{money(own)} of own share')
        for grant_id, config in self.grants.items():
            claimed = sum((line.claim for line in self.lines_for(grant_id)),
                          Fraction(0))
            if claimed > self._grant_caps[grant_id]:
                raise ClaimError(f'grant {grant_id} cap exceeded')

    @property
    def complete(self):
        return not self.unpairable

    def __getitem__(self, grant_id):
        return self.grants[grant_id]['claimed']

    def __iter__(self):
        return iter(self.grants)

    def __len__(self):
        return len(self.grants)

    def __eq__(self, other):
        if isinstance(other, Mapping):
            return dict(self) == dict(other)
        return NotImplemented

    def __hash__(self):
        return hash(tuple(sorted(self.items())))

    def lines_for(self, grant_id):
        return [line for line in self.lines if line.grant == grant_id]

    def claim_form(self, grant_id):
        """A signable claim document for one grant."""
        if grant_id not in self.grants:
            raise KeyError(grant_id)
        lines = self.lines_for(grant_id)
        payload = {
            'document': 'grant-claim',
            'grant': grant_id,
            'currency': self.currency,
            'cap': self.grants[grant_id]['cap'],
            'rate': self.grants[grant_id]['rate'],
            'purposes': self.grants[grant_id]['purposes'],
            'lines': [line.as_dict() for line in lines],
            'totals': {
                'expense': money(sum((line.amount for line in lines), Fraction(0))),
                'claim': money(sum((line.claim for line in lines), Fraction(0))),
                'own': money(sum((line.own for line in lines), Fraction(0))),
            },
        }
        return {**payload, 'digest': _digest(payload)}

    def claim_forms(self):
        """Signable claim documents for every grant that has a claim."""
        return {grant_id: self.claim_form(grant_id)
                for grant_id in self.grants
                if self.lines_for(grant_id)}

    def as_dict(self):
        return {
            'currency': self.currency,
            'lines': [line.as_dict() for line in self.lines],
            'grants': self.grants,
            'self_fund': self.self_fund,
            'totals': {'expense': self.total_expense,
                       'claimed': self.total_claimed,
                       'own': self.total_own},
            'complete': self.complete,
            'unpairable': list(self.unpairable),
            'signed_batches': [batch.as_dict() for batch in self.signed_batches],
            'claim_forms': self.claim_forms(),
        }


def _eligibility(payment, grant_map):
    return [grant_id for grant_id, config in grant_map.items()
            if grant_id in payment.eligible_grants
            and purpose_allowed(payment.purpose, config)]


def _search(unlocked, grant_map, caps, pool_total, pool_used, limit,
            forced=None):
    """Enumerate optimal assignments for unlocked payments (DFS + bound).

    ``forced`` maps payment references to the only grant they may use; it is
    applied inside the search so a requested route is either reachable as a
    global optimum or genuinely infeasible.
    """
    forced = forced or {}
    count = len(unlocked)
    options = []
    for payment in unlocked:
        eligible = _eligibility(payment, grant_map)
        if payment.reference in forced:
            wanted = forced[payment.reference]
            eligible = [wanted] if wanted in eligible else []
        options.append(eligible)

    suffix_share = [Fraction(0)] * (count + 1)
    suffix_count = [0] * (count + 1)
    for index in range(count - 1, -1, -1):
        payment = unlocked[index]
        best_here = max((min(grant_map[g]['rate'] * payment.amount,
                             payment.amount) for g in options[index]), default=Fraction(0))
        suffix_share[index] = suffix_share[index + 1] + best_here
        suffix_count[index] = suffix_count[index + 1] + (1 if best_here > 0 else 0)

    # Objective: total claimable money first, then number of funded
    # payments.  Money-first prevents a small dedicated line from displacing
    # a large claim; count-second breaks ties in favour of breadth and is
    # what keeps dedicated lines alive behind a shared payment.
    best = (Fraction(0), 0)
    found = []

    def walk(index, caps_left, used, chosen, total_share, assigned):
        nonlocal best, found
        if (total_share + suffix_share[index],
                assigned + suffix_count[index]) < best:
            return
        if index == count:
            objective = (total_share, assigned)
            if objective > best:
                best = objective
                found = [tuple(chosen)]
            elif objective == best and len(found) < limit:
                found.append(tuple(chosen))
            return

        payment = unlocked[index]
        if payment.reference not in forced:
            chosen.append((payment.reference, None, Fraction(0), payment.amount))
            walk(index + 1, caps_left, used, chosen, total_share, assigned)
            chosen.pop()

        for grant_id in options[index]:
            share = min(caps_left[grant_id],
                        Fraction(_floor(grant_map[grant_id]['rate'] * payment.amount)))
            if share <= 0:
                continue
            own = payment.amount - share
            if used + own > pool_total:
                continue
            caps_left[grant_id] -= share
            chosen.append((payment.reference, grant_id, share, own))
            walk(index + 1, caps_left, used + own, chosen,
                 total_share + share, assigned + 1)
            chosen.pop()
            caps_left[grant_id] += share

    walk(0, dict(caps), pool_used, [], Fraction(0), 0)
    return found, best


def _unpairable(payment_map, grant_map, chosen, caps_left, pool_left):
    rows = []
    for reference, payment in payment_map.items():
        if reference in chosen and chosen[reference][1] is not None:
            continue
        eligible = _eligibility(payment, grant_map)
        if not eligible:
            reason = 'no_eligible_grant'
        else:
            possible = max((min(caps_left[grant_id],
                                Fraction(_floor(grant_map[grant_id]['rate'] * payment.amount)))
                            for grant_id in eligible), default=Fraction(0))
            if possible <= 0:
                reason = 'cap_exhausted'
            elif payment.amount - possible > pool_left:
                reason = 'matching_funds'
            else:
                reason = 'not_selected'
        rows.append({'payment': reference, 'amount': money(payment.amount),
                     'reason': reason, 'eligible_grants': eligible})
    return rows


def _build(lines, grant_map, pool_total, signed, unpairable, currency):
    return Allocation(lines, grant_map, pool_total, signed, unpairable, currency)


def _prepare(expenses, grants, self_fund, signed):
    payment_map = payments(expenses)
    grant_map = _grant_config(grants)
    pool_total = _fraction(self_fund)
    if pool_total < 0:
        raise ClaimError('self_fund must not be negative')
    signed = tuple(signed)
    locks, caps_left, pool_used = _reserve_locks(signed, payment_map, grant_map)
    if pool_used > pool_total:
        raise SignedBatchConflict(
            f'matching funds of {money(pool_total)} cannot cover the '
            f'{money(pool_used)} required by signed batches; reverse and refile')
    unlocked = sorted((payment for reference, payment in payment_map.items()
                       if reference not in locks),
                      key=lambda payment: (-payment.amount, payment.reference))
    return payment_map, grant_map, pool_total, signed, locks, caps_left, pool_used, unlocked


def _assemble(assignment, payment_map, grant_map, pool_total, signed, locks,
              caps_left, pool_used, currency):
    choice = {entry[0]: entry for entry in assignment}
    lines = []
    for reference, payment in payment_map.items():
        if reference in locks:
            grant_id, claim, own, batch_id = locks[reference]
            lines.append(Line(payment=reference, purpose=payment.purpose,
                              amount=payment.amount, grant=grant_id,
                              claim=claim, own=own, line_ids=payment.line_ids,
                              locked=True, batch=batch_id))
            continue
        chosen = choice.get(reference)
        if chosen is None or chosen[1] is None:
            continue
        lines.append(Line(payment=reference, purpose=payment.purpose,
                          amount=payment.amount, grant=chosen[1],
                          claim=chosen[2], own=chosen[3],
                          line_ids=payment.line_ids))
    caps_final = dict(caps_left)
    pool_final = pool_total - pool_used
    for line in lines:
        if not line.locked:
            caps_final[line.grant] -= line.claim
            pool_final -= line.own
    unpairable = _unpairable(
        payment_map, grant_map,
        {line.payment: (None, line.grant) for line in lines},
        caps_final, pool_final)
    return _build(lines, grant_map, pool_total, signed, unpairable, currency)


def _check_preferred(preferred, payment_map, grant_map, locks):
    if not preferred:
        return {}
    for reference, grant_id in preferred.items():
        if reference not in payment_map:
            raise NoFeasibleRoute(f'unknown payment in preferred: {reference!r}')
        if grant_id not in grant_map:
            raise NoFeasibleRoute(f'unknown grant in preferred: {grant_id!r}')
        if reference in locks and locks[reference][0] != grant_id:
            raise SignedBatchConflict(
                f'payment {reference!r} is locked to grant '
                f'{locks[reference][0]} by signed batch '
                f'{locks[reference][3]}; reverse that batch and refile '
                'a new one instead of silently reallocating')
    return dict(preferred)


def allocate(expenses, grants, self_fund=0, *, signed=(), preferred=None,
             limit=200, currency='CNY'):
    """Allocate payments globally, honouring locks, caps and matching funds.

    ``self_fund`` is the museum matching money that is genuinely available
    and not occupied by another project.  ``signed`` is an iterable of
    :class:`SignedBatch` objects that must be reproduced exactly.
    ``preferred`` maps payment references to grant ids and narrows the search
    to that route; the route must still be a globally optimal legal
    allocation, so preference is never a licence to double-claim or to take a
    locally greedy route.
    """
    prepared = _prepare(expenses, grants, self_fund, signed)
    payment_map, grant_map, pool_total, signed, locks, caps_left, pool_used, unlocked = prepared
    forced = _check_preferred(preferred, payment_map, grant_map, locks)
    found, objective = _search(unlocked, grant_map, caps_left, pool_total,
                               pool_used, max(1, int(limit)), forced)
    if forced:
        unconstrained, best = _search(unlocked, grant_map, caps_left,
                                      pool_total, pool_used, 1)
        if not found or objective < best:
            raise NoFeasibleRoute(
                'the requested route is legal as a set of shares but is not a '
                'globally optimal allocation; refusing to waste claimable capacity')
    elif not found:
        raise NoFeasibleRoute('no legal allocation found')
    return _assemble(found[0], payment_map, grant_map, pool_total, signed,
                     locks, caps_left, pool_used, currency)


def scenarios(expenses, grants, self_fund=0, *, signed=(), preferred=None,
              limit=200, currency='CNY'):
    """Every globally optimal legal allocation, in deterministic order."""
    prepared = _prepare(expenses, grants, self_fund, signed)
    payment_map, grant_map, pool_total, signed, locks, caps_left, pool_used, unlocked = prepared
    forced = _check_preferred(preferred, payment_map, grant_map, locks)
    found, objective = _search(unlocked, grant_map, caps_left, pool_total,
                               pool_used, max(1, int(limit)), forced)
    if forced:
        _, best = _search(unlocked, grant_map, caps_left, pool_total,
                          pool_used, 1)
        if not found or objective < best:
            raise NoFeasibleRoute(
                'the requested route is legal as a set of shares but is not a '
                'globally optimal allocation; refusing to waste claimable capacity')
    return [_assemble(assignment, payment_map, grant_map, pool_total, signed,
                      locks, caps_left, pool_used, currency)
            for assignment in found]


def allocate_fixture(data, **kwargs):
    """Allocate a fixture document containing expenses, grants and self_fund."""
    kwargs.setdefault('self_fund', data.get('self_fund', 0))
    kwargs.setdefault('currency', data.get('currency', 'CNY'))
    return allocate(data['expenses'], data['grants'], **kwargs)


def sign_claims(allocation, issued_at=None, prefix='CLM'):
    """Sign the allocation's claim forms as one immutable batch per grant."""
    issued_at = issued_at or _now()
    batches = []
    for grant_id in allocation.grants:
        lines = allocation.lines_for(grant_id)
        if not lines:
            continue
        batch_lines = tuple((line.payment, line.amount, line.claim, line.own)
                            for line in lines)
        batch_id = f'{prefix}-{grant_id}'
        batches.append(SignedBatch(
            batch_id=batch_id, grant=grant_id, issued_at=issued_at,
            lines=batch_lines,
            digest=_batch_digest(batch_id, grant_id, issued_at, batch_lines)))
    return tuple(batches)
