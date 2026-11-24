import copy
import json
import unittest
from pathlib import Path

from bookconserve.claim_capture import (
    ClaimError,
    DuplicatePayment,
    claimable,
    payments,
)
from bookconserve.grant_allocation import (
    InvalidGrant,
    NoFeasibleRoute,
    SignedBatchConflict,
    allocate,
    allocate_fixture,
    reverse_batch,
    scenarios,
    sign_claims,
    signed_batch,
    verify_signed,
)

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'shared-grants.json'


def fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def by_grant(allocation):
    return dict(allocation)


class SeedFixtureTest(unittest.TestCase):
    """The enhanced seed: three real payments, one shared by both grants."""

    def setUp(self):
        self.data = fixture()
        self.routes = scenarios(self.data['expenses'], self.data['grants'],
                                self.data['self_fund'])

    def test_capture_is_payment_exclusive_and_purpose_aware(self):
        groups = payments(self.data['expenses'])
        self.assertEqual(set(groups), {'PAY-BIND', 'PAY-12', 'PAY-13'})
        binding = groups['PAY-BIND']
        self.assertEqual(binding.line_ids, ('binding', 'binding-journal'))
        self.assertEqual(binding.amount, 1000)
        self.assertEqual(claimable(self.data['expenses'], 'G12'),
                         self.data['expenses'][:3])

    def test_exactly_the_two_legal_routes(self):
        self.assertEqual(len(self.routes), 2)
        totals = sorted((by_grant(route) for route in self.routes),
                        key=lambda route: route['G12'])
        self.assertEqual(totals, [{'G12': 200, 'G13': 1000},
                                  {'G12': 1000, 'G13': 200}])
        for route in self.routes:
            self.assertEqual(route.total_claimed, 1200)
            self.assertEqual(route.total_own, 300)
            self.assertEqual(route.self_fund['remaining'], 0)
            self.assertTrue(route.complete)
            self.assertEqual(route.unpairable, ())

    def test_shared_payment_is_never_reported_twice(self):
        for route in self.routes:
            binding = [line for line in route.lines
                       if line.payment == 'PAY-BIND']
            self.assertEqual(len(binding), 1)
            self.assertEqual(binding[0].amount, 1000)
            self.assertEqual(binding[0].claim, 800)
            self.assertEqual(binding[0].own, 200)
            # Every payment appears in exactly one grant document.
            for grant_id in route.grants:
                seen = [line.payment for line in route.lines_for(grant_id)]
                self.assertEqual(len(seen), len(set(seen)))
            all_seen = [line.payment for line in route.lines]
            self.assertEqual(sorted(all_seen),
                             ['PAY-12', 'PAY-13', 'PAY-BIND'])
            self.assertEqual(route.total_expense, 1500)

    def test_route_binding_to_g12(self):
        route = allocate(self.data['expenses'], self.data['grants'],
                         self.data['self_fund'],
                         preferred={'PAY-BIND': 'G12'})
        self.assertEqual(dict(route), {'G12': 1000, 'G13': 200})
        lines = {line.payment: line for line in route.lines}
        self.assertEqual((lines['PAY-BIND'].grant, lines['PAY-BIND'].claim,
                          lines['PAY-BIND'].own), ('G12', 800, 200))
        self.assertEqual((lines['PAY-12'].grant, lines['PAY-12'].claim,
                          lines['PAY-12'].own), ('G12', 200, 50))
        self.assertEqual((lines['PAY-13'].grant, lines['PAY-13'].claim,
                          lines['PAY-13'].own), ('G13', 200, 50))
        self.assertEqual(route.grants['G12'],
                         {'cap': 1000, 'rate': 0.8,
                          'purposes': ['binding', 'case-12'],
                          'claimed': 1000, 'remaining': 0})
        self.assertEqual(route.grants['G13']['claimed'], 200)
        self.assertEqual(route.grants['G13']['remaining'], 800)

    def test_route_binding_to_g13(self):
        route = allocate(self.data['expenses'], self.data['grants'],
                         self.data['self_fund'],
                         preferred={'PAY-BIND': 'G13'})
        self.assertEqual(dict(route), {'G12': 200, 'G13': 1000})
        lines = {line.payment: line for line in route.lines}
        self.assertEqual((lines['PAY-BIND'].grant, lines['PAY-BIND'].claim,
                          lines['PAY-BIND'].own), ('G13', 800, 200))
        self.assertEqual((lines['PAY-12'].grant, lines['PAY-12'].claim,
                          lines['PAY-12'].own), ('G12', 200, 50))
        self.assertEqual((lines['PAY-13'].grant, lines['PAY-13'].claim,
                          lines['PAY-13'].own), ('G13', 200, 50))
        self.assertEqual(route.self_fund['used'], 300)

    def test_claim_forms_are_issuable_and_disjoint(self):
        for route in self.routes:
            forms = route.claim_forms()
            self.assertEqual(set(forms), {'G12', 'G13'})
            claimed = sum(form['totals']['claim'] for form in forms.values())
            own = sum(form['totals']['own'] for form in forms.values())
            self.assertEqual(claimed + own, route.total_expense)
            self.assertEqual(claimed, 1200)
            self.assertEqual(own, 300)
            payments_seen = [line['payment'] for form in forms.values()
                             for line in form['lines']]
            self.assertEqual(len(payments_seen), len(set(payments_seen)))

    def test_route_is_not_first_come_first_served(self):
        """Grant order must not decide the shared payment."""
        route = allocate(self.data['expenses'], self.data['grants'],
                         self.data['self_fund'])
        binding = next(line for line in route.lines
                       if line.payment == 'PAY-BIND')
        self.assertEqual(binding.claim, 800)
        self.assertEqual(route.total_claimed, 1200)
        # Both routes are exposed, and they differ in which grant owns the
        # shared payment -- a choice between global optima, not a scan order.
        self.assertEqual(len(self.routes), 2)
        owners = {next(line.grant for line in candidate.lines
                       if line.payment == 'PAY-BIND')
                  for candidate in self.routes}
        self.assertEqual(owners, {'G12', 'G13'})


class CapChangeCounterexampleTest(unittest.TestCase):
    """G12 cap 800, G13 cap 1000: naive ordering wastes claimable capacity."""

    def setUp(self):
        self.data = fixture()
        self.data['grants'][0]['cap'] = 800

    def test_global_optimum_keeps_all_dedicated_lines_claimable(self):
        routes = scenarios(self.data['expenses'], self.data['grants'],
                           self.data['self_fund'])
        self.assertEqual(len(routes), 1)
        route = routes[0]
        # Greedy G12-first would put binding on G12 for 800, exhaust its cap
        # and drop the 250 G12-only payment.  The global optimum instead puts
        # binding on G13 and keeps the dedicated G12 payment funded.
        self.assertEqual(dict(route), {'G12': 200, 'G13': 1000})
        binding = next(line for line in route.lines
                       if line.payment == 'PAY-BIND')
        self.assertEqual((binding.grant, binding.claim), ('G13', 800))
        self.assertEqual([line.payment for line in route.lines_for('G12')],
                         ['PAY-12'])
        self.assertEqual(route.total_claimed, 1200)
        self.assertEqual(route.total_own, 300)
        self.assertEqual(route.grants['G12']['remaining'], 600)

    def test_greedy_g12_first_would_lose_money(self):
        """A per-grant first-come loop is infeasible, not merely suboptimal."""
        # If binding were charged to G12 first, G12 could pay 800 and the
        # remaining cap 0 cannot cover any part of case-12's 250, so the
        # dedicated line becomes unclaimable.  The global allocator refuses
        # that route: it is not preferred, and preferring it is rejected.
        with self.assertRaises(NoFeasibleRoute):
            allocate(self.data['expenses'], self.data['grants'],
                     self.data['self_fund'], preferred={'PAY-BIND': 'G12'})

    def test_cap_shortfall_is_reported_not_hidden(self):
        self.data['grants'][0]['cap'] = 200
        route = allocate(self.data['expenses'], self.data['grants'],
                         self.data['self_fund'])
        # G12 can no longer pay 80% of the 1000 binding; binding goes to G13.
        binding = next(line for line in route.lines
                       if line.payment == 'PAY-BIND')
        self.assertEqual(binding.grant, 'G13')
        # G12 pays its 250 dedicated payment up to its 200 cap; own funds
        # cover the remaining 50, and the 20% share is still respected.
        g12 = route.lines_for('G12')
        self.assertEqual([line.payment for line in g12], ['PAY-12'])
        self.assertEqual(g12[0].claim, 200)
        self.assertEqual(g12[0].own, 50)
        self.assertEqual(route.total_claimed, 1200)


class MatchingFundsTest(unittest.TestCase):
    def setUp(self):
        self.data = fixture()

    def test_insufficient_self_fund_blocks_a_route(self):
        # Both legal routes need 300 of matching money.  With only 100 the
        # allocator funds the two dedicated payments (100 own in total) and
        # reports the shared payment as blocked by matching funds.
        route = allocate(self.data['expenses'], self.data['grants'], 100)
        self.assertFalse(route.complete)
        self.assertEqual(route.total_own, 100)
        self.assertEqual(dict(route), {'G12': 200, 'G13': 200})
        self.assertEqual([row['payment'] for row in route.unpairable],
                         ['PAY-BIND'])
        self.assertEqual(route.unpairable[0]['reason'], 'matching_funds')
        # Preferring the shared payment cannot conjure matching money.
        with self.assertRaises(NoFeasibleRoute):
            allocate(self.data['expenses'], self.data['grants'], 100,
                     preferred={'PAY-BIND': 'G12'})

    def test_zero_self_fund_funds_nothing(self):
        route = allocate(self.data['expenses'], self.data['grants'], 0)
        self.assertEqual(route.lines, ())
        self.assertEqual(route.total_claimed, 0)
        self.assertEqual(len(route.unpairable), 3)

    def test_exact_self_fund_still_funds_both(self):
        route = allocate(self.data['expenses'], self.data['grants'], 300)
        self.assertTrue(route.complete)
        self.assertEqual(route.self_fund['remaining'], 0)

    def test_self_fund_is_shared_not_per_project(self):
        route = allocate(self.data['expenses'], self.data['grants'], 150)
        self.assertEqual(route.total_own, 100)
        self.assertFalse(route.complete)
        self.assertEqual(len(route.unpairable), 1)
        with self.assertRaises(ClaimError):
            allocate(self.data['expenses'], self.data['grants'], -1)


class DuplicatePaymentTest(unittest.TestCase):
    def setUp(self):
        self.data = fixture()

    def test_identical_duplicate_rows_collapse_once(self):
        route = allocate(self.data['expenses'], self.data['grants'],
                         self.data['self_fund'])
        binding = [line for line in route.lines if line.payment == 'PAY-BIND']
        self.assertEqual(len(binding), 1)
        self.assertEqual(binding[0].line_ids, ('binding', 'binding-journal'))

    def test_conflicting_duplicate_payment_is_rejected(self):
        rows = copy.deepcopy(self.data['expenses'])
        rows[1]['amount'] = 900
        with self.assertRaises(DuplicatePayment):
            allocate(rows, self.data['grants'], self.data['self_fund'])
        with self.assertRaises(DuplicatePayment):
            payments(rows)

    def test_purpose_mismatch_removes_eligibility(self):
        rows = copy.deepcopy(self.data['expenses'])
        # case-12 is not an approved G13 purpose even though lists overlap.
        rows[2]['eligible_grants'] = ['G12', 'G13']
        route = allocate(rows, self.data['grants'], self.data['self_fund'],
                         preferred={'PAY-BIND': 'G13'})
        case12 = next(line for line in route.lines if line.payment == 'PAY-12')
        self.assertEqual(case12.grant, 'G12')

    def test_invalid_cap_and_rate(self):
        bad = copy.deepcopy(self.data['grants'])
        bad[0]['cap'] = -5
        with self.assertRaises(InvalidGrant):
            allocate(self.data['expenses'], bad, self.data['self_fund'])
        bad = copy.deepcopy(self.data['grants'])
        bad[0]['rate'] = 1.5
        with self.assertRaises(InvalidGrant):
            allocate(self.data['expenses'], bad, self.data['self_fund'])


class SignedBatchTest(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.route = allocate(self.data['expenses'], self.data['grants'],
                              self.data['self_fund'],
                              preferred={'PAY-BIND': 'G12'})
        self.batches = sign_claims(self.route, issued_at='2026-11-24T00:00:00Z')

    def test_batch_digest_and_verification(self):
        for batch in self.batches:
            self.assertTrue(verify_signed(batch))
            self.assertEqual(len(batch.digest), 64)
        tampered = signed_batch('X-G12', 'G12',
                                [{'payment': 'PAY-BIND', 'amount': 1000,
                                  'claim': 800}],
                                issued_at='2026-11-24T00:00:00Z')
        object.__setattr__(tampered, 'lines',
                           (('PAY-BIND', 1000, 900, 100),))
        with self.assertRaises(SignedBatchConflict):
            verify_signed(tampered)
        with self.assertRaises(SignedBatchConflict):
            allocate(self.data['expenses'], self.data['grants'],
                     self.data['self_fund'], signed=[tampered])

    def test_signed_batch_is_locked_and_reproduced(self):
        again = allocate(self.data['expenses'], self.data['grants'],
                         self.data['self_fund'], signed=self.batches)
        self.assertEqual(dict(again), {'G12': 1000, 'G13': 200})
        for line in again.lines:
            self.assertTrue(line.locked)
            self.assertIn(line.batch, {batch.batch_id for batch in self.batches})
        self.assertEqual(again.claim_forms()['G12']['totals']['claim'], 1000)
        # Locked payments are funded, not reported as unpairable.
        self.assertTrue(again.complete)
        self.assertEqual(again.unpairable, ())

    def test_signed_batch_cannot_be_silently_rerouted(self):
        with self.assertRaises(SignedBatchConflict):
            allocate(self.data['expenses'], self.data['grants'],
                     self.data['self_fund'], signed=self.batches,
                     preferred={'PAY-BIND': 'G13'})

    def test_signed_batch_survives_data_drift_by_blocking(self):
        changed = copy.deepcopy(self.data['expenses'])
        changed[0]['amount'] = 1200
        changed[1]['amount'] = 1200
        with self.assertRaises(SignedBatchConflict):
            allocate(changed, self.data['grants'], self.data['self_fund'],
                     signed=self.batches)

    def test_signed_batch_cap_shrink_requires_reversal(self):
        shrunk = copy.deepcopy(self.data['grants'])
        shrunk[0]['cap'] = 500
        with self.assertRaises(SignedBatchConflict):
            allocate(self.data['expenses'], shrunk, self.data['self_fund'],
                     signed=self.batches)

    def test_reversal_keeps_original_digest_then_refiles_new_batch(self):
        original = self.batches[0]
        reversal = reverse_batch(original, 'binding reassigned to G13',
                                 issued_at='2026-11-25T00:00:00Z')
        self.assertEqual(reversal['reverses'], original.digest)
        self.assertEqual(reversal['original'], original.payload())
        self.assertEqual(len(reversal['digest']), 64)
        with self.assertRaises(ClaimError):
            reverse_batch(original, '')

        # Refiling: exclude the reversed batch, choose the other route, sign
        # new batches.  The original signed digest is preserved in the
        # reversal record and is not overwritten.
        refiled_route = allocate(self.data['expenses'], self.data['grants'],
                                 self.data['self_fund'],
                                 preferred={'PAY-BIND': 'G13'})
        refiled = sign_claims(refiled_route, issued_at='2026-11-25T00:00:00Z')
        self.assertEqual(dict(refiled_route), {'G12': 200, 'G13': 1000})
        self.assertNotEqual(refiled[0].digest, original.digest)
        self.assertEqual(reversal['reverses'], original.digest)
        # Temporarily treating the old batch as still live proves the guard:
        # the old G12 batch and the new G13 batch both claim PAY-BIND.
        with self.assertRaises(SignedBatchConflict):
            allocate(self.data['expenses'], self.data['grants'],
                     self.data['self_fund'], signed=self.batches + refiled)
        # Reversal is the supported path: exclude the reversed batch, then
        # the refiled batch is accepted and reproduces the new route.
        accepted = allocate(self.data['expenses'], self.data['grants'],
                            self.data['self_fund'], signed=refiled)
        self.assertEqual(dict(accepted), {'G12': 200, 'G13': 1000})
        self.assertTrue(accepted.complete)

    def test_overlapping_signed_batches_are_rejected(self):
        extra = signed_batch('DUP-G13', 'G13',
                             [{'payment': 'PAY-BIND', 'amount': 1000,
                               'claim': 800}],
                             issued_at='2026-11-24T00:00:00Z')
        with self.assertRaises(SignedBatchConflict):
            allocate(self.data['expenses'], self.data['grants'],
                     self.data['self_fund'], signed=self.batches + (extra,))

    def test_signed_batch_matching_funds_shortfall(self):
        tighter = allocate(self.data['expenses'], self.data['grants'],
                           self.data['self_fund'])
        batches = sign_claims(tighter, issued_at='2026-11-24T00:00:00Z')
        with self.assertRaises(SignedBatchConflict):
            allocate(self.data['expenses'], self.data['grants'], 100,
                     signed=batches)


class AllocationInvariantsTest(unittest.TestCase):
    def test_shares_never_exceed_actual_expense(self):
        data = fixture()
        for pool in (300, 400, 1000):
            for route in scenarios(data['expenses'], data['grants'], pool):
                claimed_per_grant = {}
                for line in route.lines:
                    self.assertLessEqual(line.claim, line.amount)
                    claimed_per_grant[line.grant] = \
                        claimed_per_grant.get(line.grant, 0) + line.claim
                for grant_id, claimed in claimed_per_grant.items():
                    self.assertLessEqual(claimed,
                                         route.grants[grant_id]['cap'])
                self.assertEqual(route.total_claimed + route.total_own,
                                 sum(line.amount for line in route.lines))
                # One payment, one grant.
                payments_seen = [line.payment for line in route.lines]
                self.assertEqual(len(payments_seen), len(set(payments_seen)))

    def test_allocate_fixture_matches_direct_call(self):
        data = fixture()
        direct = allocate(data['expenses'], data['grants'],
                          data['self_fund'],
                          preferred={'PAY-BIND': 'G12'})
        loaded = allocate_fixture(data, preferred={'PAY-BIND': 'G12'})
        self.assertEqual(dict(direct), dict(loaded))
        self.assertEqual(direct.as_dict()['currency'], 'CNY')

    def test_unknown_preference_is_rejected(self):
        data = fixture()
        with self.assertRaises(NoFeasibleRoute):
            allocate(data['expenses'], data['grants'], data['self_fund'],
                     preferred={'NOPE': 'G12'})
        with self.assertRaises(NoFeasibleRoute):
            allocate(data['expenses'], data['grants'], data['self_fund'],
                     preferred={'PAY-BIND': 'G99'})


if __name__ == '__main__':
    unittest.main()
