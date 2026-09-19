import ctypes as C
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import dahua_37777 as D


class FakeNetSDK:
    def __init__(self, login_ok=True, media_bytes=b"DHAV" + b"x" * 4096):
        self.login_ok = login_ok
        self.media_bytes = media_bytes
        self.saved_path = None
        self.stopped_save = 0
        self.stopped_play = 0
        self.logged_out = 0

    def CLIENT_GetLastError(self):
        return 0 if self.login_ok else 0x80000064

    def CLIENT_LoginWithHighLevelSecurity(self, pin_ptr, pout_ptr):
        if not self.login_ok:
            pout = C.cast(pout_ptr, C.POINTER(D.NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY)).contents
            pout.nError = 1
            return 0
        pout = C.cast(pout_ptr, C.POINTER(D.NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY)).contents
        pout.stuDeviceInfo.nChanNum = 1
        return 101

    def CLIENT_RealPlayEx(self, login, channel, hwnd, real_type):
        self.channel = channel
        self.real_type = real_type
        return 202

    def CLIENT_SaveRealData(self, real, path):
        self.saved_path = os.fsdecode(path)
        with open(self.saved_path, "wb") as handle:
            handle.write(self.media_bytes)
        return 1

    def CLIENT_StopSaveRealData(self, real):
        self.stopped_save += 1
        return 1

    def CLIENT_StopRealPlayEx(self, real):
        self.stopped_play += 1
        return 1

    def CLIENT_Logout(self, login):
        self.logged_out += 1
        return 1


class Dahua37777Tests(unittest.TestCase):
    def test_find_netsdk_dir_accepts_resolved_sdk_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk_dir = os.path.join(tmp, "dahua_netsdk")
            os.makedirs(sdk_dir)
            dll_path = os.path.join(sdk_dir, "dhnetsdk.dll")
            with open(dll_path, "wb") as handle:
                handle.write(b"dll")
            self.assertEqual(D.find_netsdk_dir(sdk_dir), os.path.abspath(sdk_dir))

    def test_camera_config_is_one_based_but_sdk_channel_is_zero_based(self):
        fake = FakeNetSDK()
        camera = {
            "ip": "127.0.0.1",
            "user": "admin",
            "pass": "secret",
            "netsdk_port": 37777,
            "netsdk_channel": 3,
            "netsdk_stream": "sub",
        }
        with patch.object(D, "_load_runtime", return_value=fake):
            adapter = D.Dahua37777Adapter.from_camera(camera)
        self.assertEqual(adapter.channel, 2)
        self.assertEqual(adapter.port, 37777)
        self.assertEqual(adapter.stream, "sub")

    def test_probe_requires_real_media_not_only_login(self):
        fake = FakeNetSDK()
        with patch.object(D, "_load_runtime", return_value=fake):
            adapter = D.Dahua37777Adapter("127.0.0.1", "admin", "secret")
            result = adapter.probe(require_media=True, media_seconds=0.01)
        self.assertTrue(result.ok)
        self.assertTrue(result.media_ok)
        self.assertEqual(result.channels, 1)
        self.assertGreaterEqual(fake.stopped_play, 1)
        self.assertGreaterEqual(fake.logged_out, 1)

    def test_probe_reports_auth_failure_without_secret(self):
        fake = FakeNetSDK(login_ok=False)
        secret = "do-not-leak-this"
        with patch.object(D, "_load_runtime", return_value=fake):
            adapter = D.Dahua37777Adapter("127.0.0.1", "admin", secret)
            result = adapter.probe(require_media=True, media_seconds=0.01)
        self.assertFalse(result.ok)
        self.assertEqual(result.sdk_error, 0x80000064)
        self.assertIn("NET_LOGIN_ERROR_PASSWORD", result.message)
        self.assertNotIn(secret, result.message)

    def test_raw_dvrip_normal_challenge_login(self):
        realm = "Login to Device"
        random_value = "12345678"
        username = "admin"
        password = "normal-device-password"
        expected = (
            username
            + "&&"
            + D._dahua_gen2_md5(random_value, realm, username, password)
            + D._dahua_dvrip_md5(random_value, username, password)
        ).encode("latin-1")

        server = __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        failures = []

        def recv_exact(conn, length):
            data = b""
            while len(data) < length:
                chunk = conn.recv(length - len(data))
                if not chunk:
                    break
                data += chunk
            return data

        def serve():
            try:
                conn, _ = server.accept()
                with conn:
                    realm_header = recv_exact(conn, 32)
                    self.assertEqual(realm_header[:4], bytes.fromhex("a0010000"))
                    payload = f"Realm:{realm}\r\nRandom:{random_value}\r\n".encode("latin-1")
                    reply = bytearray(32)
                    reply[:4] = bytes.fromhex("b0010068")
                    reply[4:8] = len(payload).to_bytes(4, "little")
                    reply[16:20] = len(payload).to_bytes(4, "little")
                    conn.sendall(bytes(reply) + payload)

                    login_header = recv_exact(conn, 32)
                    length = int.from_bytes(login_header[4:8], "little")
                    challenge = recv_exact(conn, length)
                    self.assertEqual(login_header[:4], bytes.fromhex("a0050000"))
                    self.assertEqual(challenge, expected)

                    success = bytearray(32)
                    success[:4] = bytes.fromhex("b0000068")
                    success[8:12] = bytes.fromhex("00080000")
                    success[16:20] = (1234).to_bytes(4, "little")
                    success[24:28] = bytes.fromhex("0600f900")
                    success[28:32] = bytes.fromhex("00010000")
                    conn.sendall(bytes(success))
            except Exception as exc:
                failures.append(exc)
            finally:
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        result = D.probe_dvrip_auth("127.0.0.1", username, password, port=port, timeout=2)
        thread.join(2)
        if failures:
            raise failures[0]
        self.assertTrue(result.ok)
        self.assertEqual(result.error_code[:4], "0008")
        self.assertEqual(result.session_id, 1234)
        self.assertNotIn(password, result.message)

    def test_record_segment_uses_realplay_and_removes_dav(self):
        fake = FakeNetSDK()
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "clip.mp4")
            with patch.object(D, "_load_runtime", return_value=fake):
                adapter = D.Dahua37777Adapter("127.0.0.1", "admin", "secret")
                def convert(dav, out, ffmpeg):
                    self.assertTrue(os.path.exists(dav))
                    with open(out, "wb") as handle:
                        handle.write(b"mp4-data")
                    return "copy"
                with patch.object(adapter, "_dav_to_mp4", side_effect=convert):
                    result = adapter.record_segment(output, 0.01, "ffmpeg", stop)
            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "copy")
            self.assertTrue(os.path.exists(output))
            self.assertFalse(os.path.exists(output + ".netsdk.dav"))


if __name__ == "__main__":
    unittest.main()
