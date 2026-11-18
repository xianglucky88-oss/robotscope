import unittest

from tests.support import make_state
from robotscope.lease import LeaseManager


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.state, self.time = make_state()
        self.leases = LeaseManager(self.state, global_limit=2, host_limit=1, lease_ttl=100)

    def test_slots_are_global_and_per_host(self):
        a = self.leases.acquire("q1", "a", now=10.0)
        self.assertTrue(a.granted)
        blocked = self.leases.acquire("q2", "a", now=10.0)
        self.assertFalse(blocked.granted)
        self.assertEqual(blocked.reason, "host_slots_full")
        other = self.leases.acquire("q3", "b", now=10.0)
        self.assertTrue(other.granted)
        full = self.leases.acquire("q4", "c", now=10.0)
        self.assertFalse(full.granted)
        self.assertEqual(full.reason, "global_slots_full")

    def test_duplicate_acquire_does_not_take_a_second_slot(self):
        first = self.leases.acquire("q1", "a", now=10.0)
        again = self.leases.acquire("q1", "a", now=11.0)
        self.assertTrue(again.granted)
        self.assertTrue(again.duplicate)
        self.assertEqual(again.lease_id, first.lease_id)
        self.assertEqual(self.leases.slot_usage()["global"], 1)

    def test_release_frees_slot(self):
        lease = self.leases.acquire("q1", "a", now=10.0)
        self.assertTrue(self.leases.release(lease.lease_id, now=11.0))
        self.assertEqual(self.leases.slot_usage()["global"], 0)
        self.assertFalse(self.leases.release(lease.lease_id, now=12.0))

    def test_expiry_and_stale_worker(self):
        lease = self.leases.acquire("q1", "a", now=10.0, ttl=50)
        # Before expiry the same worker may execute.
        self.assertTrue(self.leases.validate(lease.lease_id, request_id="q1",
                                             fence=lease.fence, now=20.0))
        self.time.set(100.0)
        reclaimed = self.leases.reclaim()
        self.assertEqual([r["lease_id"] for r in reclaimed], [lease.lease_id])
        # The old worker's fence is now stale.
        self.assertFalse(self.leases.validate(lease.lease_id, request_id="q1",
                                              fence=lease.fence, now=101.0))
        self.assertFalse(self.leases.renew(lease.lease_id, fence=lease.fence, now=101.0))

    def test_replacement_worker_gets_new_fence(self):
        first = self.leases.acquire("q1", "a", now=10.0, ttl=50)
        self.time.set(100.0)
        self.leases.reclaim()
        second = self.leases.acquire("q1", "a", now=100.0)
        self.assertTrue(second.granted)
        self.assertGreater(second.fence, first.fence)
        self.assertFalse(self.leases.validate(first.lease_id, request_id="q1",
                                              fence=first.fence, now=101.0))
        self.assertTrue(self.leases.validate(second.lease_id, request_id="q1",
                                             fence=second.fence, now=101.0))

    def test_renew_extends_without_changing_fence(self):
        lease = self.leases.acquire("q1", "a", now=10.0, ttl=50)
        self.assertTrue(self.leases.renew(lease.lease_id, fence=lease.fence, ttl=200, now=30.0))
        self.assertTrue(self.leases.validate(lease.lease_id, request_id="q1",
                                             fence=lease.fence, now=200.0))

    def test_wrong_fence_cannot_renew(self):
        lease = self.leases.acquire("q1", "a", now=10.0)
        self.assertFalse(self.leases.renew(lease.lease_id, fence=lease.fence + 1, now=20.0))


if __name__ == "__main__":
    unittest.main()
