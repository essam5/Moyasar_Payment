import hashlib
import hmac
import json
import logging
from decimal import Decimal

import graphene
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from saleor.payment import ChargeStatus
from saleor.payment.interface import GatewayResponse, PaymentData, PaymentMethodInfo

from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.payment.gateway import payment_refund_or_void
from saleor.checkout.calculations import calculate_checkout_total_with_gift_cards
from saleor.checkout.complete_checkout import complete_checkout
from django.core.exceptions import ValidationError
from saleor.discount.utils import fetch_active_discounts

# Get the logger for this file, it will allow us to log error responses from checkout.
logger = logging.getLogger(__name__)


def _success_response(
    kind: str,
    payment_response: dict,
    is_success: bool = True,
    token=None,
    amount=None,
    currency=None,
    customer_id=None,
    raw_response=None,
    action_required=True,
    action_required_data: dict = None,
):
    return GatewayResponse(
        kind=kind,
        error=None,
        amount=amount,
        currency=currency,
        transaction_id=token,
        is_success=is_success,
        customer_id=customer_id,
        action_required=action_required,
        action_required_data=action_required_data,
        raw_response=raw_response or payment_response,
        payment_method_info=PaymentMethodInfo(type="card"),
    )


def _error_response(
    exc,
    kind: str,
    payment_info: PaymentData,
    raw_response: dict = None,
    action_required: bool = False,
) -> GatewayResponse:
    return GatewayResponse(
        error=exc,
        kind=kind,
        is_success=False,
        raw_response=raw_response,
        amount=payment_info.amount,
        currency=payment_info.currency,
        action_required=action_required,
        customer_id=payment_info.customer_id,
        transaction_id=str(payment_info.token),
        payment_method_info=PaymentMethodInfo(type="card"),
    )


def get_payment_customer_id(payment_information):
    from saleor.account.models import User

    pk = User.objects.filter(email=payment_information.customer_email).first().id
    return graphene.Node.to_global_id("User", pk) if pk else ""


def create_order_extend(payment, checkout, manager):
    try:
        discounts = fetch_active_discounts()
        lines, unavailable_variant_pks = fetch_checkout_lines(checkout)
        if unavailable_variant_pks:
            payment_refund_or_void(payment, manager, checkout.channel.slug)
            raise ValidationError(
                "Some of the checkout lines variants are unavailable."
            )
        checkout_info = fetch_checkout_info(checkout, lines, discounts, manager)
        checkout_total = calculate_checkout_total_with_gift_cards(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            address=checkout.shipping_address or checkout.billing_address,
            discounts=discounts,
        )
        # when checkout total value is different than total amount from payments
        # it means that some products has been removed during the payment was completed
        if checkout_total.gross.amount != payment.total:
            payment_refund_or_void(payment, manager, checkout_info.channel.slug)
            raise ValidationError(
                "Cannot create order - some products do not exist anymore."
            )
        order, _, _ = complete_checkout(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            payment_data={},
            store_source=False,
            discounts=discounts,
            user=checkout.user or None,
            app=None,
        )
    except ValidationError as e:
        logger.info(
            "Failed to create order from checkout %s.", checkout.pk, extra={"error": e}
        )
        return None
    # Refresh the payment to assign the newly created order
    payment.refresh_from_db()
    return order


def verify_webhook(request: HttpRequest, secret_key):
    h = hmac.new(
        msg=request.body,
        digestmod=hashlib.sha256,
        key=secret_key.encode("utf-8"),
    ).hexdigest()
    if h != request.headers.get("Cko-Signature", ""):
        return HttpResponseForbidden()
    return True


def handle_webhook(request: HttpRequest, config, gateway: str):
    secret_key = config.connection_params.get("private_key", None)
    data_from_moyasar = json.loads(request.body.decode("utf-8").replace("'", '"'))
    # Verify the webhook signature.
    if verify_webhook(request=request, secret_key=secret_key) is True:
        if data_from_moyasar.get("type") == "payment_paid":
            payment_data = data_from_moyasar.get("data", {})
            if payment_data:
                payment_id = payment_data.get("id", None)
                from saleor.payment.models import Payment

                payment = Payment.objects.filter(
                    token=payment_id, gateway=gateway
                ).last()
                if payment is not None:
                    if payment.checkout:
                        # Create the order into the database
                        from saleor.plugins.manager import get_plugins_manager

                        order = create_order_extend(
                            payment=payment,
                            checkout=payment.checkout,
                            manager=get_plugins_manager(),
                        )

                        if order:
                            # Mark the payment as paid
                            amount = Decimal(payment_data.get("amount")) / 100
                            payment.captured_amount = amount
                            payment.charge_status = (
                                ChargeStatus.FULLY_CHARGED
                                if amount >= payment.total
                                else ChargeStatus.PARTIALLY_CHARGED
                            )
                            payment.save(
                                update_fields=[
                                    "charge_status",
                                    "captured_amount",
                                ]
                            )

                            # Remove the unneeded payments from the database.
                            for p in payment.checkout.payments.exclude(id=payment.id):
                                p.transactions.all().delete()
                                p.delete()

                            logger.info(
                                msg=f"Order #{order.id} created",
                                extra={"order_id": order.id},
                            )
                            return HttpResponse("OK", status=200)

                return HttpResponse("Payment not found", status=200)
