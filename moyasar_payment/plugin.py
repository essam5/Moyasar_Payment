import logging

import requests
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponseNotFound
from django.utils.translation import gettext_lazy as _
from saleor.graphql.core.enums import PluginErrorCode

from saleor.payment.gateways.utils import (
    get_supported_currencies,
    require_active_plugin,
)
from saleor.payment.interface import GatewayConfig, GatewayResponse, PaymentData
from saleor.plugins.base_plugin import BasePlugin, ConfigurationTypeField
from saleor.plugins.models import PluginConfiguration

logger = logging.getLogger(__name__)

import requests
from .constants import MOYASAR_API_BASE_URL, GATEWAY_NAME
from saleor.payment import TransactionKind
from checkout_payment.utils import (
    handle_webhook,
    _error_response,
    _success_response,
    get_payment_customer_id,
)
from saleor.payment.models import Payment
import base64


class MoyasarPaymentPlugin(BasePlugin):
    PLUGIN_NAME = GATEWAY_NAME
    PLUGIN_ID = "moyasar_payment"
    CONFIGURATION_PER_CHANNEL = False

    DEFAULT_CONFIGURATION = [
        {
            "name": "public_api_key",
            "value": "sk_test_9FcaoMeU3FUB787UJq7fP68TnzP4xaeq2amiVFFq",
        },
        {
            "name": "secret_api_key",
            "value": "sk_test_9FcaoMeU3FUB787UJq7fP68TnzP4xaeq2amiVFFq",
        },
        {"name": "supported_currencies", "value": "SAR"},
    ]

    CONFIG_STRUCTURE = {
        "public_api_key": {
            "type": ConfigurationTypeField.SECRET,
            "help_text": "Provide  public API key",
            "label": "Public API key",
        },
        "secret_api_key": {
            "type": ConfigurationTypeField.SECRET,
            "help_text": "Provide Moyasar secret API key",
            "label": "Secret API key",
        },
        "supported_currencies": {
            "type": ConfigurationTypeField.STRING,
            "help_text": "Supported Currencies for Moyasar",
            "label": "Supported Currencies",
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configuration = {item["name"]: item["value"] for item in self.configuration}
        self.config = GatewayConfig(
            auto_capture=True,
            gateway_name=GATEWAY_NAME,
            connection_params={
                "public_key": configuration["public_api_key"],
                "private_key": configuration["secret_api_key"],
            },
            supported_currencies=configuration["supported_currencies"],
        )

    def _get_gateway_config(self):
        return self.config

    @classmethod
    def validate_plugin_configuration(cls, plugin_configuration: "PluginConfiguration"):
        """Validate if provided configuration is correct."""

        missing_fields = []
        configuration = plugin_configuration.configuration
        configuration = {item["name"]: item["value"] for item in configuration}
        if not configuration["public_api_key"]:
            missing_fields.append("public_api_key")
        if not configuration["secret_api_key"]:
            missing_fields.append("secret_api_key")

        if plugin_configuration.active and missing_fields:
            error_msg = (
                "To enable a plugin, you need to provide values for the "
                "following fields: "
            )
            raise ValidationError(
                {
                    f"{field}": ValidationError(
                        error_msg.format(field),
                        code=PluginErrorCode.PLUGIN_MISCONFIGURED.value,
                    )
                    for field in missing_fields
                },
            )

    def get_client_token(self):
        return self.config.connection_params.get("public_key")

    @require_active_plugin
    def get_supported_currencies(self, previous_value):
        return get_supported_currencies(self.config, self.PLUGIN_NAME)

    @require_active_plugin
    def process_payment(
        self, payment_information: "PaymentData", previous_value
    ) -> "GatewayResponse":
        config = self._get_gateway_config()

        api_key = config.connection_params["public_key"]
        payment_q = Payment.objects.get(
            pk=payment_information.payment_id, is_active=True
        )

        # Extract the "amount," "currency," and "description" attributes
        amount = payment_q.captured_amount
        currency = payment_q.currency

        # Check if Apple Pay option is selected
        use_apple_pay = payment_q.get_value_from_metadata("use_apple_pay")

        if use_apple_pay:
            # Handle Apple Pay payment flow
            apple_pay_data = payment_q.get_value_from_metadata("moyasar_data")

            # Update the payment data with the extracted attributes
            payment_data.update(
                {
                    "amount": int(payment_information.amount),
                    "currency": currency,
                }
            )

            base64_encoded_key = base64.b64encode(api_key.encode()).decode()

            # Create a payment request to Moyasar
            headers = {
                "Authorization": f"Basic {base64_encoded_key}:",
                "Content-Type": "application/json",
            }

            try:
                response = requests.post(
                    f"{MOYASAR_API_BASE_URL}/payments",
                    headers=headers,
                    json=apple_pay_data,
                ).json()

                # Handle the response and return the payment status
                if response.get("id"):
                    payment_status = "confirmed with Apple Pay"
                else:
                    payment_status = "failed with Apple Pay"

                return payment_status

            except requests.exceptions.RequestException:
                return "error"

        else:
            # Extract the payment data from the metadata
            payment_data = payment_q.get_value_from_metadata("moyasar_data")

            # Update the payment data with the extracted attributes
            payment_data.update(
                {
                    "amount": int(payment_information.amount),
                    "currency": currency,
                }
            )

            base64_encoded_key = base64.b64encode(api_key.encode()).decode()

            # Create a payment request to Moyasar
            headers = {
                "Authorization": f"Basic {base64_encoded_key}:",
                "Content-Type": "application/json",
            }

            try:
                response = requests.post(
                    f"{MOYASAR_API_BASE_URL}/payments",
                    headers=headers,
                    json=payment_data,
                ).json()

                # Handle the response and return the payment status
                if response.get("id"):
                    payment_status = "confirmed"
                    is_3ds_url = (
                        response.redirect_link.get("href")
                        if hasattr(response, "redirect_link")
                        else None
                    )
                    process_response = _success_response(
                        token=response.get("id"),
                        amount=payment_information.amount,
                        currency=payment_information.currency,
                        payment_response=response,
                        customer_id=get_payment_customer_id(payment_information),
                        is_success=True,
                        kind=TransactionKind.AUTH
                        if is_3ds_url
                        else TransactionKind.CAPTURE,
                        action_required_data={
                            "3ds_url": is_3ds_url,
                        },
                    )
                else:
                    payment_status = "failed"
                    process_response = _error_response(
                        exc=payment_status,
                        action_required=True,
                        kind=TransactionKind.AUTH,
                        payment_info=payment_information,
                        raw_response=response,
                    )

                return process_response

            except requests.exceptions.RequestException:
                return _error_response(
                    exc="faild",
                    action_required=True,
                    kind=TransactionKind.AUTH,
                    payment_info=payment_information,
                )

    @require_active_plugin
    def capture_payment(
        self, payment_information: "PaymentData", previous_value
    ) -> "GatewayResponse":
        config = self._get_gateway_config()

        api_key = config.connection_params["public_key"]
        payment_id = payment_information.token

        base64_encoded_key = base64.b64encode(api_key.encode()).decode()

        # Create a payment request to Moyasar
        headers = {
            "Authorization": f"Basic {base64_encoded_key}:",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                f"{MOYASAR_API_BASE_URL}/payments/{payment_id}/capture",
                headers=headers,
                json={},
            ).json()

            # Handle the response and return the capture status
            if response.get("id"):
                process_response = _success_response(
                    token=response.get("id"),
                    amount=payment_information.amount,
                    currency=payment_information.currency,
                    payment_response=response,
                    customer_id=get_payment_customer_id(payment_information),
                    is_success=True,
                    kind=TransactionKind.CAPTURE,
                )
            else:
                payment_status = "failed"
                process_response = _error_response(
                    exc=payment_status,
                    action_required=True,
                    kind=TransactionKind.CAPTURE,
                    payment_info=payment_information,
                    raw_response=response,
                )

            return process_response

        except requests.exceptions.RequestException:
            return _error_response(
                exc="failed to capture",
                action_required=True,
                kind=TransactionKind.AUTH,
                payment_info=payment_information,
            )

    @require_active_plugin
    def refund_payment(
        self, payment_information: "PaymentData", previous_value
    ) -> "GatewayResponse":
        config = self._get_gateway_config()

        api_key = config.connection_params["public_key"]
        payment_id = payment_information.token

        base64_encoded_key = base64.b64encode(api_key.encode()).decode()

        # Create a payment request to Moyasar
        headers = {
            "Authorization": f"Basic {base64_encoded_key}:",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                f"{MOYASAR_API_BASE_URL}/payments/{payment_id}/refund",
                headers=headers,
                json={},
            ).json()

            # Handle the response and return the refund status
            if response.get("id"):
                process_response = _success_response(
                    token=response.get("id"),
                    amount=payment_information.amount,
                    currency=payment_information.currency,
                    payment_response=response,
                    customer_id=get_payment_customer_id(payment_information),
                    is_success=True,
                    kind=TransactionKind.REFUND,
                )
            else:
                payment_status = "failed"
                process_response = _error_response(
                    exc=payment_status,
                    action_required=True,
                    kind=TransactionKind.REFUND,
                    payment_info=payment_information,
                    raw_response=response,
                )

            return process_response

        except requests.exceptions.RequestException:
            return _error_response(
                exc="failed to refund",
                action_required=True,
                kind=TransactionKind.REFUND,
                payment_info=payment_information,
            )

    @require_active_plugin
    def confirm_payment(
        self, payment_information: "PaymentData", previous_value
    ) -> "GatewayResponse":
        config = self._get_gateway_config()

        api_key = config.connection_params["public_key"]
        payment_id = payment_information.token
        base64_encoded_key = base64.b64encode(api_key.encode()).decode()

        # Create a payment request to Moyasar
        headers = {
            "Authorization": f"Basic {base64_encoded_key}:",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                f"{MOYASAR_API_BASE_URL}/payments/{payment_id}/confirm",
                headers=headers,
                json={},
            ).json()

            # Handle the response and return the confirm status
            if response.get("id"):
                process_response = _success_response(
                    token=response.get("id"),
                    amount=payment_information.amount,
                    currency=payment_information.currency,
                    payment_response=response,
                    customer_id=get_payment_customer_id(payment_information),
                    is_success=True,
                    kind=TransactionKind.REFUND,
                )
            else:
                payment_status = "failed"
                process_response = _error_response(
                    exc=payment_status,
                    action_required=True,
                    kind=TransactionKind.REFUND,
                    payment_info=payment_information,
                    raw_response=response,
                )

            return process_response

        except requests.exceptions.RequestException:
            return _error_response(
                exc="failed to confirm",
                action_required=True,
                kind=TransactionKind.AUTH,
                payment_info=payment_information,
            )

    @require_active_plugin
    def get_payment_config(self, previous_value):
        config = self._get_gateway_config()
        return [{"field": "api_key", "value": config.connection_params["public_key"]}]

    def webhook(self, request: HttpRequest, path: str, *args, **kwargs):
        if path == "/paid/" and request.method == "POST":
            response = handle_webhook(
                request=request,
                gateway=self.PLUGIN_ID,
                config=self._get_gateway_config(),
            )
            logger.info(msg="Finish handling webhook")
            return response

        return HttpResponseNotFound("This path is not valid!")
