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
        self.assertEqual(self.c.get("/").status_code, 401)
        bad = {"Authorization": "Basic " + base64.b64encode(b"me:wrong").decode()}
        self.assertEqual(self.c.get("/", headers=bad).status_code, 401)
        self.assertEqual(self.c.get("/healthz").status_code, 200)

    def test_empty_then_run_now_saves_history(self):
        self.assertIn("No briefings yet", self.c.get("/", headers=self.auth).get_data(as_text=True))
        with mock.patch.object(web, "run_once", return_value=dict(FAKE)) as ro:
            r = self.c.post("/run", headers=self.auth, data={})
            self.assertEqual(r.status_code, 303)
            for _ in range(50):
                if not web._state["running"] and web.list_runs():
                    break
                time.sleep(0.05)
        ro.assert_called_once_with(push=False)
        page = self.c.get("/", headers=self.auth).get_data(as_text=True)
        self.assertIn("Payrolls rose 29,000", page)
        self.assertIn("1 unverified link", page)
        self.assertIn("https://made.up/x", page)
        run_id = web.list_runs()[0]["id"]
        self.assertEqual(self.c.get(f"/b/{run_id}", headers=self.auth).status_code, 200)
        self.assertEqual(self.c.get("/b/../etc", headers=self.auth).status_code, 404)

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
