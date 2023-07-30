import unittest
from unittest.mock import patch
from .moyasar_payment.plugin import MoyasarPaymentPlugin


class TestMoyasarPaymentPlugin(unittest.TestCase):
    def setUp(self):
        self.plugin = MoyasarPaymentPlugin()
        self.payment_data = {
            "amount": 200,
            "currency": "SAR",
            "description": "Order #123 Payment",
        }

    @patch("requests.post")
    def test_process_payment_success(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.status_code = 200

        result = self.plugin.process_payment(self.payment_data, None)

        self.assertEqual(result, "confirmed")

    @patch("requests.post")
    def test_process_payment_failure(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.status_code = 500

        result = self.plugin.process_payment(self.payment_data, None)

        self.assertEqual(result, "failed")

    @patch("requests.post")
    def test_capture_payment_success(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.status_code = 200

        result = self.plugin.capture_payment({"payment_id": "mock_payment_id"}, None)

        self.assertEqual(result, "captured")

    @patch("requests.post")
    def test_capture_payment_failure(self, mock_post):
        mock_response = mock_post.return_value
        mock_response.status_code = 500

        result = self.plugin.capture_payment({"payment_id": "mock_payment_id"}, None)

        self.assertEqual(result, "failed")


if __name__ == "__main__":
    unittest.main()
