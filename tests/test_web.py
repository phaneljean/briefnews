"""python -m unittest discover tests"""
import base64, os, shutil, sys, tempfile, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(SCHEDULER="0", DASHBOARD_PASSWORD="pw-test", GOOGLE_API_KEY="")
import web  # noqa: E402

FAKE = {"briefing": "## 1. Core Policy & Economy\n- Payrolls rose 29,000. ([CNBC](https://cnbc.com/x))",
        "model": "gemini-test", "items": 64, "sources": ["BBC World", "CNBC Economy"],
        "flagged_links": ["https://made.up/x"], "beehiiv": None}


class WebTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        web.HISTORY_DIR = self.dir
        self.c = web.app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"me:pw-test").decode()}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_password_required(self):
        self.assertEqual(self.c.get("/admin").status_code, 401)
        self.assertEqual(self.c.post("/run").status_code, 401)
        bad = {"Authorization": "Basic " + base64.b64encode(b"me:wrong").decode()}
        self.assertEqual(self.c.get("/admin", headers=bad).status_code, 401)
        for public in ["/", "/archive"]:
            self.assertEqual(self.c.get(public).status_code, 200, public)
        self.assertEqual(self.c.get("/healthz").status_code, 200)

    def test_empty_then_run_now_saves_history(self):
        self.assertIn("No briefings yet", self.c.get("/admin", headers=self.auth).get_data(as_text=True))
        with mock.patch.object(web, "run_once", return_value=dict(FAKE)) as ro:
            r = self.c.post("/run", headers=self.auth, data={})
            self.assertEqual(r.status_code, 303)
            for _ in range(50):
                if not web._state["running"] and web.list_runs():
                    break
                time.sleep(0.05)
        ro.assert_called_once_with(push=False)
        page = self.c.get("/admin", headers=self.auth).get_data(as_text=True)
        self.assertIn("Payrolls rose 29,000", page)
        self.assertIn("1 unverified link", page)
        self.assertIn("https://made.up/x", page)
        run_id = web.list_runs()[0]["id"]
        self.assertEqual(self.c.get(f"/admin/b/{run_id}", headers=self.auth).status_code, 200)
        self.assertEqual(self.c.get("/admin/b/../etc", headers=self.auth).status_code, 404)

    def test_public_site_hides_flagged_issues(self):
        clean = dict(FAKE, flagged_links=[], briefing=(
            "## 1. Core Policy & Economy\n- U.S. payrolls rose 29,000 in September. The jobless rate hit 4.2%. "
            "([CNBC Economy](https://cnbc.com/x))\n"
            "## 4. The Noise Filter\n- Left out: A viral clip — it changed no votes."))
        flagged_id = web.save_run(dict(FAKE), "schedule")
        time.sleep(1.1)
        clean_id = web.save_run(clean, "schedule")
        home = self.c.get("/").get_data(as_text=True)
        self.assertIn("<strong>U.S. payrolls rose 29,000 in September.</strong>", home)
        self.assertIn("left out today", home)
        self.assertIn("A viral clip", home)
        self.assertEqual(self.c.get(f"/issue/{clean_id}").status_code, 200)
        self.assertEqual(self.c.get(f"/issue/{flagged_id}").status_code, 404)
        self.assertNotIn(flagged_id, self.c.get("/archive").get_data(as_text=True))

    def test_subscribe(self):
        with mock.patch.object(web, "subscribe_to_beehiiv") as sub:
            r = self.c.post("/subscribe", data={"email": "Reader@Example.com"})
            self.assertIn("s=ok", r.headers["Location"])
            sub.assert_called_once_with("reader@example.com")
            r = self.c.post("/subscribe", data={"email": "not-an-email"})
            self.assertIn("s=err", r.headers["Location"])
            self.c.post("/subscribe", data={"email": "bot@example.com", "website": "spam.biz"})
            self.assertEqual(sub.call_count, 1)  # honeypot: silently ignored
        with mock.patch.object(web, "subscribe_to_beehiiv", side_effect=RuntimeError("Try later")):
            r = self.c.post("/subscribe", data={"email": "x@example.com"})
        self.assertIn("s=err", r.headers["Location"])

    def test_runs_never_overlap(self):
        started = []
        def slow(push):
            started.append(push); time.sleep(0.3); return dict(FAKE)
        with mock.patch.object(web, "run_once", side_effect=slow):
            import threading
            t = threading.Thread(target=web.job, args=("a", False)); t.start()
            time.sleep(0.05)
            web.job("b", False)  # should skip, not run concurrently
            t.join()
        self.assertEqual(len(started), 1)


if __name__ == "__main__":
    unittest.main()
