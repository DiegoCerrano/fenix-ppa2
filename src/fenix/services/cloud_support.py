"""Short Boto3 recipes for EC2 and S3 (the new parameters, ready to use)."""

from botocore.exceptions import ClientError

from fenix.utils.errors import ConfigError


def aws_tags(tags):
    """Convert a tag dict into the list format AWS expects.

    Args:
        tags: Dict like ``{"Owner": "grupo01"}``.

    Returns:
        A list like ``[{"Key": "Owner", "Value": "grupo01"}]`` sorted by
        key.

    Raises:
        None.
    """

    return [{"Key": key, "Value": value} for key, value in sorted(tags.items())]


def tags_to_dict(tag_list):
    """Convert AWS tag lists back into a plain dict.

    Args:
        tag_list: A list of ``{"Key", "Value"}`` items, or None.

    Returns:
        A dict of tags (empty when tag_list is None or empty).

    Raises:
        None.
    """

    return {item["Key"]: item["Value"] for item in tag_list or []}


def error_code(error):
    """Extract the AWS error code from a ClientError.

    Args:
        error: A ``botocore.exceptions.ClientError``.

    Returns:
        The code (for example ``"NoSuchBucket"``) or ``""`` if absent.

    Raises:
        None.
    """

    return error.response.get("Error", {}).get("Code", "")


def mock_inputs(ec2):
    """Discover a VPC and an image id available in the emulator.

    Args:
        ec2: A Boto3 EC2 client.

    Returns:
        A tuple ``(vpc_id, image_id)``.

    Raises:
        ConfigError: If LocalStack has no VPC or no image.
        ClientError: If the EC2 API call itself fails.
    """

    vpcs = ec2.describe_vpcs(
        Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        vpcs = ec2.describe_vpcs()["Vpcs"]
    images = ec2.describe_images()["Images"]
    if not vpcs or not images:
        raise ConfigError("LocalStack no tiene VPC o AMI: revisa el entorno.")
    return vpcs[0]["VpcId"], images[0]["ImageId"]


def create_bucket(s3, name):
    """Create an S3 bucket (tagging is a separate call).

    Args:
        s3: A Boto3 S3 client.
        name: Bucket name.

    Returns:
        None.

    Raises:
        ClientError: If the bucket cannot be created.

    Side Effects:
        Creates the bucket. If a later ``put_tags`` fails, the bucket
        already exists: a partial failure the caller must handle.
    """
    region = s3.meta.region_name
    if region and region != "us-east-1":
        s3.create_bucket(Bucket=name, CreateBucketConfiguration={
            "LocationConstraint": region})
    else:
        s3.create_bucket(Bucket=name)


def bucket_exists(s3, name):
    """Tell whether a bucket exists.

    Args:
        s3: A Boto3 S3 client.
        name: Bucket name.

    Returns:
        True if the bucket exists, False if S3 says it does not.

    Raises:
        ClientError: For any error other than "not found".
    """
    try:
        s3.head_bucket(Bucket=name)
    except ClientError as error:
        if error_code(error) in ("404", "NoSuchBucket", "NotFound"):
            return False
        raise
    return True


def get_bucket_tags(s3, name):
    """Read the current tags of a bucket.

    Args:
        s3: A Boto3 S3 client.
        name: Bucket name.

    Returns:
        A dict of tags. ``{}`` when the bucket has no tag set yet.

    Raises:
        ClientError: For any error other than ``NoSuchTagSet``.
    """
    try:
        return tags_to_dict(s3.get_bucket_tagging(Bucket=name)["TagSet"])
    except ClientError as error:
        if error_code(error) == "NoSuchTagSet":
            return {}
        raise


def put_tags(s3, name, tags):
    """Write the COMPLETE tag set of a bucket.

    Args:
        s3: A Boto3 S3 client.
        name: Bucket name.
        tags: Dict with every tag the bucket must have afterwards.

    Returns:
        None.

    Raises:
        ClientError: If S3 rejects the call.

    Side Effects:
        Replaces the whole previous tag set, so always read and merge
        first.
    """
    s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": aws_tags(tags)})


def create_group(ec2, name, vpc_id, tags):
    """Create a security group with no inbound rules.
    Args:
        ec2: A Boto3 EC2 client.
        name: Group name.
        vpc_id: VPC where the group is created.
        tags: Dict of tags to apply.

    Returns:
        The new ``GroupId``.

    Raises:
        ClientError: If EC2 rejects the call.
    """
    response = ec2.create_security_group(
        GroupName=name,
        Description="Grupo exclusivo del laboratorio Fenix",
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group",
                            "Tags": aws_tags(tags)}],
    )
    return response["GroupId"]


def create_instance(ec2, image_id, group_id, tags, token):
    """Request one mock instance, idempotently.

    Args:
        ec2: A Boto3 EC2 client.
        image_id: Image to launch.
        group_id: Security group id.
        tags: Dict of tags to apply.
        token: ``ClientToken``; repeating the call with the same token
            does not create a second instance.

    Returns:
        The ``InstanceId``.

    Raises:
        ClientError: If EC2 rejects the call.
    """
    response = ec2.run_instances(
        ImageId=image_id, MinCount=1, MaxCount=1, InstanceType="t3.micro",
        SecurityGroupIds=[group_id], ClientToken=token,
        TagSpecifications=[{"ResourceType": "instance",
                            "Tags": aws_tags(tags)}],
    )
    return response["Instances"][0]["InstanceId"]


def instance_state(ec2, instance_id):
    """Read the real state of an instance.

    Args:
        ec2: A Boto3 EC2 client.
        instance_id: Instance id to look up.

    Returns:
        The state name (``running``, ``stopped``...), or None if the
        instance does not exist.

    Raises:
        ClientError: For any error other than "invalid instance id".
    """
    try:
        response = ec2.describe_instances(InstanceIds=[instance_id])
    except ClientError as error:
        if error_code(error).startswith("InvalidInstanceID"):
            return None
        raise
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            return instance["State"]["Name"]
    return None
