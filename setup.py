from setuptools import setup

setup(
    name="moyasar-payment",
    version="0.1.1",
    packages=["moyasar_payment"],
    package_dir={"moyasar_payment": "moyasar_payment"},
    description="moyasar payment plugin",
    entry_points={
        "saleor.plugins": [
            "moyasar_payment = moyasar_payment.plugin:MoyasarPaymentPlugin"
        ],
    },
)
