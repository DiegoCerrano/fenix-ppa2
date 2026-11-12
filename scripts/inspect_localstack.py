#!/usr/bin/env python3
"""List, and optionally purge, Fenix resources living inside LocalStack.

Standalone on purpose: it only needs boto3, so it works from the very
first work session, before the fenix package has any module.

Usage:
    python scripts/inspect_localstack.py         # list everything
    python scripts/inspect_localstack.py --purge # delete Fenix resources
"""

import argparse
import os
import sys
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def endpoint():
    """Return the LocalStack endpoint, refusing anything but loopback:4566."""
    url = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
    parsed = urlparse(url)
    if parsed.hostname not in LOOPBACK or parsed.port != 4566:
        sys.exit("AWS_ENDPOINT_URL debe apuntar a loopback:4566")
    return url


def client(service):
    """Create a Boto3 client for LocalStack with dummy credentials."""
    options = {
        "connect_timeout": 2,
        "read_timeout": 10,
        "retries": {"max_attempts": 1},
    }
    if service == "s3":
        options["s3"] = {"addressing_style": "path"}
    return boto3.client(
        service,
        endpoint_url=endpoint(),
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(**options),
    )


def tag_dict(tags):
    """Convert an AWS tag list to a dict."""
    return {t["Key"]: t["Value"] for t in tags or []}


def collect():
    """Gather every queue, bucket, instance and security group."""
    sqs, s3, ec2 = client("sqs"), client("s3"), client("ec2")
    found = {"queues": [], "buckets": [], "instances": [], "groups": []}

    for url in sqs.list_queues().get("QueueUrls", []):
        attrs = sqs.get_queue_attributes(
            QueueUrl=url, AttributeNames=["All"]
        )["Attributes"]
        found["queues"].append(
            {
                "name": url.rsplit("/", 1)[-1],
                "url": url,
                "messages": attrs.get("ApproximateNumberOfMessages", "?"),
                "redrive": "RedrivePolicy" in attrs,
            }
        )

    for bucket in s3.list_buckets().get("Buckets", []):
        try:
            tags = tag_dict(
                s3.get_bucket_tagging(Bucket=bucket["Name"])["TagSet"]
            )
        except ClientError:
            tags = {}
        found["buckets"].append({"name": bucket["Name"], "tags": tags})

    for res in ec2.describe_instances()["Reservations"]:
        for item in res["Instances"]:
            found["instances"].append(
                {
                    "id": item["InstanceId"],
                    "state": item["State"]["Name"],
                    "tags": tag_dict(item.get("Tags")),
                }
            )

    for group in ec2.describe_security_groups()["SecurityGroups"]:
        if group["GroupName"] != "default":
            found["groups"].append(
                {
                    "id": group["GroupId"],
                    "name": group["GroupName"],
                    "tags": tag_dict(group.get("Tags")),
                }
            )

    return found


def is_fenix_instance(item):
    """Tell whether an instance belongs to the Fenix lab."""
    tags = item["tags"]
    return tags.get("Project") == "fenix" or tags.get("ManagedBy") == "ppa2"


def show(found):
    """Print a human readable report."""
    print(f"Endpoint: {endpoint()}")
    print(f"\nColas SQS ({len(found['queues'])}):")
    for q in found["queues"]:
        print(
            f"  - {q['name']} mensajes={q['messages']} redrive={'si' if q['redrive'] else 'no'}"
        )

    print(f"\nBuckets S3 ({len(found['buckets'])}):")
    for b in found["buckets"]:
        print(f"  - {b['name']} tags={b['tags']}")

    print(f"\nInstancias EC2 ({len(found['instances'])}):")
    for i in found["instances"]:
        mark = "fenix" if is_fenix_instance(i) else "ajena"
        print(
            f"  - {i['id']} {i['state']} [{mark}] GroupId={i['tags'].get('GroupId', '-')}"
        )

    print(f"\nGrupos de seguridad ({len(found['groups'])}):")
    for g in found["groups"]:
        print(f"  - {g['id']} {g['name']}")


def purge(found):
    """Delete the resources that look like Fenix ones."""
    import time

    sqs, s3, ec2 = client("sqs"), client("s3"), client("ec2")

    for q in found["queues"]:
        if q["name"].startswith("fenix-"):
            sqs.delete_queue(QueueUrl=q["url"])
            print("  cola borrada:", q["name"])

    ids = [i["id"] for i in found["instances"] if is_fenix_instance(i)]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        time.sleep(3)
        print("  instancias terminadas:", ids)

    for b in found["buckets"]:
        if not b["name"].startswith("fenix-"):
            continue
        listing = s3.list_objects_v2(Bucket=b["name"])
        keys = [{"Key": o["Key"]} for o in listing.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=b["name"], Delete={"Objects": keys})
        s3.delete_bucket(Bucket=b["name"])
        print("  bucket borrado:", b["name"])

    for g in found["groups"]:
        if not g["name"].startswith("fenix-sg"):
            continue
        for _ in range(6):
            try:
                ec2.delete_security_group(GroupId=g["id"])
                print("  grupo borrado:", g["name"])
                break
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code", "")
                if code != "DependencyViolation":
                    raise
                time.sleep(1)


def main():
    """Run the script."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--purge", action="store_true", help="borrar los recursos de Fenix"
    )
    parser.add_argument(
        "--yes", action="store_true", help="no pedir confirmación"
    )
    args = parser.parse_args()

    try:
        found = collect()
    except (BotoCoreError, ClientError) as error:
        print(
            f"No pude hablar con LocalStack ({type(error).__name__}). ¿Está corriendo? docker compose up -d"
        )
        return 1

    show(found)

    if args.purge:
        if not args.yes:
            answer = input("\nEscribí BORRAR para eliminar lo de Fenix: ")
            if answer.strip() != "BORRAR":
                print("Cancelado.")
                return 0
        purge(found)
        print("\nListo. Estado después de borrar:")
        show(collect())

    return 0


if __name__ == "__main__":
    sys.exit(main())
