import unittest
from unittest.mock import MagicMock, patch

from services import live_streamer, network


class SharedServicesTests(unittest.TestCase):
    def test_lan_lookup_closes_socket_when_connect_fails(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.connect.side_effect = OSError("offline")
        with patch.object(network.socket, "socket", return_value=sock), \
                patch.object(network.socket, "gethostbyname", return_value="192.168.1.8"):
            self.assertEqual(network.get_local_lan_ip(), "192.168.1.8")
        sock.__exit__.assert_called_once()

    def test_lan_lookup_closes_socket_on_success(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = ("192.168.1.9", 123)
        with patch.object(network.socket, "socket", return_value=sock):
            self.assertEqual(network.get_local_lan_ip(), "192.168.1.9")
        sock.__exit__.assert_called_once()

    def test_live_preview_emits_multipart_and_releases_on_disconnect(self):
        cv = MagicMock()
        cap = cv.VideoCapture.return_value
        cap.isOpened.return_value = True
        cap.grab.return_value = True
        cap.retrieve.return_value = (True, object())
        buffer = MagicMock()
        buffer.tobytes.return_value = b"jpeg"
        cv.imencode.return_value = (True, buffer)
        with patch.object(live_streamer, "cv2", cv), \
                patch.dict(live_streamer.os.environ):
            stream = live_streamer.gen_frames("rtsp://fixture")
            self.assertEqual(next(stream), b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\njpeg\r\n")
            stream.close()
        cap.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
