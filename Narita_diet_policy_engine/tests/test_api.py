import http.client
import json
import threading
import unittest
from pathlib import Path

from app.simple_server import make_server

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = json.loads((ROOT / "examples" / "noncompliant_plan.json").read_text())

try:
    from fastapi.testclient import TestClient

    from app.main import app
except ImportError:  # FastAPI not installed
    TestClient = None


class StdlibServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload

    def test_health_and_listings(self):
        self.assertEqual(self.request("GET", "/health"), (200, {"status": "ok"}))
        status, policies = self.request("GET", "/policies")
        self.assertEqual(status, 200)
        self.assertIn("diabetic_ccho", {p["id"] for p in policies})
        self.assertEqual(self.request("GET", "/policies/diabetic_ccho")[0], 200)
        self.assertEqual(self.request("GET", "/policies/nope")[0], 404)
        self.assertGreater(len(self.request("GET", "/foods")[1]), 20)

    def test_validate_noncompliant(self):
        status, report = self.request("POST", "/validate", EXAMPLE)
        self.assertEqual(status, 200)
        self.assertFalse(report["compliant"])
        self.assertEqual(report["summary"]["errors"], 4)

    def test_davao_policy_listed_and_validates(self):
        policies = {p["id"]: p for p in self.request("GET", "/policies")[1]}
        self.assertEqual(policies["davao_hospital_diabetic"]["status"], "template")
        plan = json.loads((ROOT / "examples" / "davao_compliant_plan.json").read_text())
        status, report = self.request("POST", "/validate", plan)
        self.assertEqual(status, 200)
        self.assertTrue(report["fully_compliant"])

    def test_validate_errors(self):
        self.assertEqual(self.request("POST", "/validate", {"policy_id": "nope", "meals": []})[0], 404)
        bad_food = {"policy_id": "low_sodium_2g", "meals": [{"name": "L", "items": [{"food_id": "zzz"}]}]}
        self.assertEqual(self.request("POST", "/validate", bad_food)[0], 422)
        self.assertEqual(self.request("POST", "/validate", raw="{not json")[0], 400)
        self.assertEqual(self.request("POST", "/elsewhere", {})[0], 404)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class FastAPITests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_validate_noncompliant(self):
        r = self.client.post("/validate", json=EXAMPLE)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["compliant"])
        self.assertEqual(r.json()["summary"]["errors"], 4)

    def test_validate_compliant(self):
        plan = json.loads((ROOT / "examples" / "compliant_plan.json").read_text())
        r = self.client.post("/validate", json=plan)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["fully_compliant"])

    def test_davao_plan(self):
        plan = json.loads((ROOT / "examples" / "davao_noncompliant_plan.json").read_text())
        r = self.client.post("/validate", json=plan)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["summary"]["errors"], 5)

    def test_error_statuses(self):
        self.assertEqual(self.client.post("/validate", json={"policy_id": "nope", "meals": [
            {"name": "L", "items": [{"food_id": "banana"}]}]}).status_code, 404)
        self.assertEqual(self.client.post("/validate", json={"policy_id": "low_sodium_2g", "meals": [
            {"name": "L", "items": [{"food_id": "zzz"}]}]}).status_code, 422)
        self.assertEqual(self.client.post("/validate", json={"policy_id": "low_sodium_2g"}).status_code, 422)

    def test_listings(self):
        self.assertEqual(self.client.get("/policies").status_code, 200)
        self.assertEqual(self.client.get("/policies/nope").status_code, 404)
        self.assertGreater(len(self.client.get("/foods").json()), 20)


if __name__ == "__main__":
    unittest.main()
