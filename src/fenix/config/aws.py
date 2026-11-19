"""Boto3 clients for LocalStack, built from the values in ``settings``.

Kept apart from ``settings`` so that reading the environment does not
require Boto3, and so that tests have a single place to patch:
``fenix.config.aws.get_client``.
"""

import boto3
from botocore.config import Config

from fenix.config import settings
from fenix.utils.errors import ConfigError

SERVICES = ("sqs", "s3", "ec2")


def get_client(service):
    """Create a Boto3 client that always talks to LocalStack.

    Args:
        service: Service name: ``"sqs"``, ``"s3"`` or ``"ec2"``.

    Returns:
        A configured Boto3 client with bounded timeouts.

    Raises:
        ConfigError: If the service is not one of ``SERVICES``, the
            endpoint is not loopback:4566 or the access key looks like a
            real AWS key (see ``settings.endpoint_url`` and
            ``settings.aws_credentials``).

    Notes:
        Connect timeout is 2 s. Read timeout is 25 s for SQS (long polling
        lasts 10 s) and 10 s for the rest. S3 uses path-style addressing
        because LocalStack needs it.
    """

    if service not in SERVICES:
        raise ConfigError(
            f"Servicio desconocido {service!r}: usá sqs, s3 o ec2")
    key, secret = settings.aws_credentials()
    options = {
        "connect_timeout": 2,
        "read_timeout": 25 if service == "sqs" else 10,
        "retries": {"max_attempts": 2, "mode": "standard"},
    }
    if service == "s3":
        options["s3"] = {"addressing_style": "path"}
    return boto3.client(
        service,
        endpoint_url=settings.endpoint_url(),
        region_name=settings.region(),
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=Config(**options),
    )
