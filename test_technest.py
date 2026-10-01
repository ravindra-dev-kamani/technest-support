"""Automated tests for TechNest Support. Run:  python -m unittest -v"""
import os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.resolve()))
_tmp = tempfile.mkdtemp()
os.environ["TECHNEST_DB"] = os.path.join(_tmp, "test.db")  # throwaway database
os.environ.pop("GEMINI_API_KEY", None)                      # tests use the offline rule engine
os.environ.pop("GOOGLE_API_KEY", None)
os.chdir(_tmp)                                              # uploads/ is created here, not in the project
import technest_agent as t


def make(kind="refund", amount=500, price=1000, days=3):
    c = t.Complaint(0, t.secrets.token_urlsafe(6), "Ram", "12 MG Road, Rewa", "9876543210", "ram@example.com",
                    "INV-1", "2026-09-25", price, "test complaint text", kind=kind, amount=amount, days=days)
    t.save(c)
    return c


def tick():
    """Run the SLA timer exactly once."""
    with mock.patch.object(t.time, "sleep", side_effect=[None, KeyboardInterrupt]):
        try:
            t.sla_loop()
        except KeyboardInterrupt:
            pass


def age(c, seconds):
    t.CON.execute("UPDATE cases SET updated=? WHERE id=?", (time.time() - seconds, c.id))
    t.CON.commit()


class Tests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(t, "send_email")  # no real emails while testing
        self.mail = p.start()
        self.addCleanup(p.stop)

    def test_staff_powers(self):
        riya, amit, neha = t.STAFF
        c = make(amount=3000)
        self.assertFalse(t.within_power(riya, c))
        self.assertTrue(t.within_power(amit, c))
        self.assertTrue(t.within_power(neha, c))

    def test_keyword_triage(self):
        r = t.regex_triage("mujhe paisa wapas karo ya new kapde do ₹250")
        self.assertEqual((r["kind"], r["amount"]), ("refund", "250"))
        self.assertEqual(t.regex_triage("5,00,000 rupees ka clothes")["amount"], "500000")
        self.assertEqual(t.regex_triage("please replace this")["kind"], "exchange")

    def test_refund_never_exceeds_purchase(self):
        c = make(price=1000)
        t.triage(c, "refund", 5000)
        self.assertEqual(c.amount, 1000)

    def test_small_refund_resolved_at_level_1(self):
        c = make(amount=400)
        t.run_agent(c)
        self.assertEqual((c.level, c.status), (0, "AWAITING_CUSTOMER"))

    def test_big_refund_needs_manager_approval(self):
        c = make(amount=12000, price=12000)
        t.run_agent(c)
        self.assertEqual((c.level, c.status), (1, "PENDING_APPROVAL"))

    def test_head_caps_compensation(self):
        c = make(kind="compensation", amount=90000, price=90000)
        t.run_agent(c)
        self.assertEqual(c.level, 2)
        self.assertIn("₹50,000", c.offer)

    def test_rejections_escalate_then_close(self):
        c = make(amount=400)
        t.run_agent(c)
        t.customer_reply(c, False)
        self.assertEqual(c.level, 1)
        self.assertIn("goodwill voucher", c.offer)
        t.customer_reply(c, False)
        t.customer_reply(c, False)
        self.assertEqual(c.status, "CLOSED_BY_HEAD")

    def test_only_owner_or_head_can_approve(self):
        c = make(amount=12000, price=12000)
        t.run_agent(c)
        self.assertFalse(t.staff_action(c, "riya", "approve"))
        self.assertTrue(t.staff_action(c, "amit", "approve"))
        d = make(amount=12000, price=12000)
        t.run_agent(d)
        self.assertTrue(t.staff_action(d, "neha", "approve"))

    def test_ai_cannot_approve_beyond_power(self):
        c = make(amount=12000, price=12000)
        with mock.patch.object(t, "llm_json", return_value={"action": "resolve", "message": "Sure!"}):
            t.run_agent(c)
        self.assertGreaterEqual(c.level, 1)  # Riya could not resolve it, whatever the AI said

    def test_ai_may_escalate_early(self):
        c = make(amount=400)
        with mock.patch.object(t, "llm_json", return_value={"action": "escalate", "message": "x"}):
            t.run_agent(c)
        self.assertEqual(c.level, 2)  # the head is the last stop and always answers

    def test_slow_staff_are_auto_escalated(self):
        c = make(amount=12000, price=12000)
        t.run_agent(c)
        age(c, t.APPROVAL_SLA + 10)
        tick()
        c = t.load(c.id)
        self.assertEqual((c.level, c.status), (2, "AWAITING_CUSTOMER"))

    def test_silent_customer_case_expires(self):
        c = make(amount=400)
        t.run_agent(c)
        age(c, t.CUSTOMER_SLA + 10)
        tick()
        self.assertEqual(t.load(c.id).status, "EXPIRED")

    def test_validation(self):
        _, errs = t.validate({"name": "R", "mobile": "123", "email": "bad", "address": "x", "text": "short"})
        self.assertGreaterEqual(len(errs), 6)
        ok = {"name": "Ram Kumar", "mobile": "+91 98765 43210", "email": "ram@x.com", "address": "12 MG Road Rewa",
              "bill_no": "A1", "purchase_date": "2026-09-01", "purchase_amount": "500", "text": "screen is cracked"}
        clean, errs = t.validate(ok)
        self.assertEqual((errs, clean["mobile"]), ([], "9876543210"))

    def test_customer_emailed_once_per_status_change(self):
        c = make(amount=400)
        t.run_agent(c)
        self.assertEqual(self.mail.call_args[0][0], "ram@example.com")
        n = self.mail.call_count
        t.save(c)
        self.assertEqual(self.mail.call_count, n)


if __name__ == "__main__":
    unittest.main()
