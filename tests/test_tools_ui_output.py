import base64
import unittest

from mcp_server import tools_ui


class ScreenshotResultTests(unittest.TestCase):
    def test_format_result_uses_connector_safe_jpeg(self):
        result = tools_ui._format_result({"ok": True}, b"jpeg-probe")

        self.assertIsInstance(result, list)
        self.assertEqual(2, len(result))
        image = result[1].to_image_content()
        self.assertEqual("image/jpeg", image.mimeType)
        self.assertEqual(b"jpeg-probe", base64.b64decode(image.data))

    def test_connector_screenshot_limits_are_explicit(self):
        self.assertEqual("jpeg", tools_ui._SCREENSHOT_FORMAT)
        self.assertEqual(1600, tools_ui._SCREENSHOT_MAX_DIMENSION)
        self.assertEqual(600_000, tools_ui._SCREENSHOT_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
