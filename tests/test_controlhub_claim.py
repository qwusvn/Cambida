import json
from pathlib import Path
import tempfile
import unittest

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
    def test_subdomain_claim_writes_token_and_binding(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text(json.dumps({
                "server_port": 8004,
                "keep": True,
                "cloudflare_subdomain": "Q1.Example.com",
                "controlhub_url": "https://central.example.com",
            }), encoding="utf-8")
            (base / "RELEASE_VERSION.txt").write_text("2.3.0\n", encoding="utf-8")

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
            self.assertEqual(seen["url"], "https://central.example.com/api/cambida/claim-by-subdomain")
            self.assertEqual(seen["body"]["subdomain"], "q1.example.com")
            self.assertTrue(seen["body"]["machine_id"])
            self.assertEqual(seen["body"]["app_version"], "2.3.0")
            self.assertEqual(seen["body"]["server_port"], 8004)

            cfg = json.loads((base / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["cloudflare_subdomain"], "q1.example.com")
            self.assertEqual(cfg["cloudflare_bound_subdomain"], "q1.example.com")
            self.assertEqual(cfg["cloudflare_bound_port"], 8004)
            self.assertEqual(cfg["public_base_url"], "https://q1.example.com")
            self.assertTrue(cfg["keep"])
            self.assertEqual((base / "tunnel_token.txt").read_text(encoding="utf-8").strip(), "shop-tunnel-token")
            self.assertTrue((base / CLAIM.MACHINE_ID_FILE).is_file())

    def test_bound_subdomain_reuses_token_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text(json.dumps({
                "cloudflare_subdomain": "q1.example.com",
                "cloudflare_bound_subdomain": "q1.example.com",
                "cloudflare_bound_port": 8000,
                "server_port": 8000,
                "public_base_url": "https://q1.example.com",
            }), encoding="utf-8")
            (base / "tunnel_token.txt").write_text("token\n", encoding="utf-8")
            result = CLAIM.claim_once(base, opener=lambda *a, **k: self.fail("network should not be used"))
            self.assertEqual(result["status"], "already-provisioned")

    def test_changed_port_refreshes_ingress_binding(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text(json.dumps({
                "cloudflare_subdomain": "q1.example.com",
                "cloudflare_bound_subdomain": "q1.example.com",
                "cloudflare_bound_port": 8004,
                "server_port": 8000,
                "public_base_url": "https://q1.example.com",
                "controlhub_url": "https://central.example.com",
            }), encoding="utf-8")
            (base / "tunnel_token.txt").write_text("old-token\n", encoding="utf-8")
            seen = {}
            def opener(req, timeout=0):
                seen["body"] = json.loads(req.data.decode("utf-8"))
                return _Response({
                    "public_base_url": "https://q1.example.com",
                    "tunnel_token": "new-token",
                })
            CLAIM.claim_once(base, opener=opener)
            self.assertEqual(seen["body"]["server_port"], 8000)
            cfg = json.loads((base / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["cloudflare_bound_port"], 8000)

    def test_changed_subdomain_does_not_reuse_old_token(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text(json.dumps({
                "cloudflare_subdomain": "q2.example.com",
                "cloudflare_bound_subdomain": "q1.example.com",
                "public_base_url": "https://q2.example.com",
                "controlhub_url": "https://central.example.com",
            }), encoding="utf-8")
            (base / "tunnel_token.txt").write_text("old-token\n", encoding="utf-8")
            seen = {}
            def opener(req, timeout=0):
                seen["called"] = True
                return _Response({
                    "public_base_url": "https://q2.example.com",
                    "tunnel_token": "new-token",
                })
            CLAIM.claim_once(base, opener=opener)
            self.assertTrue(seen["called"])
            self.assertEqual((base / "tunnel_token.txt").read_text(encoding="utf-8").strip(), "new-token")

    def test_legacy_bootstrap_remains_supported(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text('{"server_port":8004}', encoding="utf-8")
            (base / CLAIM.BOOTSTRAP_FILE).write_text(json.dumps({
                "controlhub_url": "https://central.example.com",
                "project_id": "cambida",
                "site_id": "site-1",
                "claim_token": "once",
            }), encoding="utf-8")
            def opener(req, timeout=0):
                self.assertEqual(req.full_url, "https://central.example.com/api/claim")
                return _Response({
                    "public_base_url": "https://legacy.example.com",
                    "tunnel_token": "legacy-token",
                })
            result = CLAIM.claim_once(base, opener=opener)
            self.assertTrue(result["claimed"])
            cfg=json.loads((base / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["cloudflare_subdomain"], "legacy.example.com")
            self.assertFalse((base / CLAIM.BOOTSTRAP_FILE).exists())

    def test_fresh_231_client_registers_and_waits_for_assignment(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text(json.dumps({
                "server_port": 8000,
                "controlhub_url": "https://central.example.com",
            }), encoding="utf-8")
            (base / "RELEASE_VERSION.txt").write_text("2.3.1\n", encoding="utf-8")
            seen = []

            def opener(req, timeout=0):
                body = json.loads(req.data.decode("utf-8"))
                seen.append((req.full_url, body))
                return _Response({
                    "status": "waiting_assignment",
                    "pairing_code": "A1B2C3D4",
                    "machine_id": body["machine_id"],
                })

            first = CLAIM.claim_once(base, opener=opener)
            second = CLAIM.claim_once(base, opener=opener)
            self.assertEqual(first["status"], "waiting-assignment")
            self.assertEqual(first["pairing_code"], "A1B2C3D4")
            self.assertEqual(seen[0][0], "https://central.example.com/api/cambida/register-client")
            self.assertEqual(seen[0][1]["app_version"], "2.3.1")
            self.assertEqual(seen[0][1]["server_port"], 8000)
            self.assertEqual(seen[0][1]["machine_id"], seen[1][1]["machine_id"])
            self.assertEqual(seen[0][1]["client_secret"], seen[1][1]["client_secret"])
            self.assertTrue((base / CLAIM.MACHINE_ID_FILE).is_file())
            self.assertTrue((base / CLAIM.CLIENT_SECRET_FILE).is_file())
            self.assertFalse((base / CLAIM.TOKEN_FILE).exists())

    def test_assigned_231_client_saves_subdomain_and_tunnel_token(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text(json.dumps({
                "server_port": 8000,
                "controlhub_url": "https://central.example.com",
                "keep": True,
            }), encoding="utf-8")
            (base / "RELEASE_VERSION.txt").write_text("2.3.1\n", encoding="utf-8")

            def opener(req, timeout=0):
                self.assertEqual(req.full_url, "https://central.example.com/api/cambida/register-client")
                return _Response({
                    "status": "assigned",
                    "pairing_code": "A1B2C3D4",
                    "subdomain": "auto.hhan24.org",
                    "public_base_url": "https://auto.hhan24.org",
                    "tunnel_token": "auto-tunnel-token",
                    "server_port": 8000,
                })

            result = CLAIM.claim_once(base, opener=opener)
            self.assertTrue(result["claimed"])
            self.assertEqual(result["subdomain"], "auto.hhan24.org")
            cfg = json.loads((base / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["cloudflare_subdomain"], "auto.hhan24.org")
            self.assertEqual(cfg["cloudflare_bound_subdomain"], "auto.hhan24.org")
            self.assertEqual(cfg["cloudflare_bound_port"], 8000)
            self.assertEqual(cfg["public_base_url"], "https://auto.hhan24.org")
            self.assertTrue(cfg["keep"])
            self.assertEqual((base / CLAIM.TOKEN_FILE).read_text(encoding="utf-8").strip(), "auto-tunnel-token")

    def test_rejects_plain_http_remote_controlhub(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "config.json").write_text(json.dumps({
                "cloudflare_subdomain": "q1.example.com",
                "controlhub_url": "http://central.example.com",
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                CLAIM.claim_once(base)


if __name__ == "__main__":
    unittest.main()
