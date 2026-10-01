import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import controlhub_claim as CLAIM


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload


class ControlHubClaimTests(unittest.TestCase):
    def test_claim_writes_config_token_and_removes_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text('{"server_port": 8004, "keep": true}', encoding="utf-8")
            (base / "RELEASE_VERSION.txt").write_text("2.2.6\n", encoding="utf-8")
            (base / CLAIM.BOOTSTRAP_FILE).write_text(json.dumps({
                "schema": 1,
                "controlhub_url": "https://central.example.com",
                "project_id": "cambida",
                "site_id": "site-1",
                "claim_token": "once-only",
            }), encoding="utf-8")

            seen = {}
            def opener(req, timeout=0):
                seen["url"] = req.full_url
                seen["body"] = json.loads(req.data.decode("utf-8"))
                return _Response({
                    "public_base_url": "https://q1.example.com",
                    "tunnel_token": "shop-tunnel-token",
                })

            result = CLAIM.claim_once(base, opener=opener)
            self.assertTrue(result["claimed"])
            self.assertEqual(seen["url"], "https://central.example.com/api/claim")
            self.assertEqual(seen["body"]["project_id"], "cambida")
            self.assertEqual(seen["body"]["site_id"], "site-1")
            self.assertTrue(seen["body"]["machine_id"])
            self.assertEqual(seen["body"]["app_version"], "2.2.6")

            cfg = json.loads((base / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["public_base_url"], "https://q1.example.com")
            self.assertTrue(cfg["keep"])
            self.assertEqual((base / "tunnel_token.txt").read_text(encoding="utf-8").strip(), "shop-tunnel-token")
            self.assertFalse((base / CLAIM.BOOTSTRAP_FILE).exists())
            self.assertTrue((base / CLAIM.MACHINE_ID_FILE).is_file())

    def test_existing_provisioning_discards_stale_bootstrap_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text('{"public_base_url":"https://q1.example.com"}', encoding="utf-8")
            (base / "tunnel_token.txt").write_text("token\n", encoding="utf-8")
            (base / CLAIM.BOOTSTRAP_FILE).write_text("{}", encoding="utf-8")
            result = CLAIM.claim_once(base, opener=lambda *a, **k: self.fail("network should not be used"))
            self.assertEqual(result["status"], "already-provisioned")
            self.assertFalse((base / CLAIM.BOOTSTRAP_FILE).exists())

    def test_rejects_plain_http_remote_controlhub(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / CLAIM.BOOTSTRAP_FILE).write_text(json.dumps({
                "controlhub_url": "http://central.example.com",
                "project_id": "cambida",
                "site_id": "site-1",
                "claim_token": "once",
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                CLAIM.claim_once(base)


if __name__ == "__main__":
    unittest.main()
