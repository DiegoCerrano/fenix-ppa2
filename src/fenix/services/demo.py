"""Lab helpers: build the inventory, provoke faults and clean up."""

import time
import uuid

from botocore.exceptions import ClientError

from fenix.config import aws, settings
from fenix.services import cloud_support as cloud
from fenix.utils import state
from fenix.utils.errors import ConfigError, ValidationError
from fenix.utils.logging_json import get_logger

log = get_logger("demo")


def expected_tags(group):
    """Build the mandatory tags of every resource of the group.

    Args:
        group: Group identifier.

    Returns:
        A dict with ``ManagedBy``, ``Project``, ``GroupId`` and ``Owner``.

    Raises:
        None.
    """
    return {"ManagedBy": "ppa2", "Project": "fenix",
            "GroupId": group, "Owner": group}


def _wait_state(ec2, instance_id, wanted, tries=15):
    """Wait (bounded, about 15 s) for an instance to reach a state.

    Args:
        ec2: A Boto3 EC2 client.
        instance_id: Instance to watch.
        wanted: Target state name.
        tries: Maximum number of one-second checks.

    Returns:
        The last state observed (equal to wanted on success).

    Raises:
        ClientError: If EC2 fails with an unexpected error.
    """
    current = None
    for _ in range(tries):
        current = cloud.instance_state(ec2, instance_id)
        if current == wanted:
            return current
        time.sleep(1)
    return current


def _find_group(ec2, name, group):
    """Find a security group by name without adopting foreign ones.

    Args:
        ec2: A Boto3 EC2 client.
        name: Security group name.
        group: Our group identifier.

    Returns:
        The ``GroupId`` if it exists and is ours, otherwise None.

    Raises:
        ConfigError: If a group with that name belongs to another group.
        ClientError: If EC2 fails.
    """
    found = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}])["SecurityGroups"]
    if not found:
        return None
    tags = cloud.tags_to_dict(found[0].get("Tags"))
    if tags.get("GroupId", group) != group:
        raise ConfigError(f"El SG {name} pertenece a otro grupo")
    return found[0]["GroupId"]


def _find_instance(ec2, group, alias):
    """Find a live instance of ours by tags.

    Args:
        ec2: A Boto3 EC2 client.
        group: Our group identifier.
        alias: Resource alias (for example ``node-01``).

    Returns:
        The ``InstanceId`` or None.

    Raises:
        ClientError: If EC2 fails.
    """
    filters = [
        {"Name": "tag:ManagedBy", "Values": ["ppa2"]},
        {"Name": "tag:GroupId", "Values": [group]},
        {"Name": "tag:Alias", "Values": [alias]},
        {"Name": "instance-state-name",
         "Values": ["pending", "running", "stopping", "stopped"]},
    ]
    for reservation in ec2.describe_instances(Filters=filters)["Reservations"]:
        for instance in reservation["Instances"]:
            return instance["InstanceId"]
    return None


def _setup_node(ec2, inventory, group, tags):
    """Create or reuse the security group and the instance ``node-01``.

    Args:
        ec2: A Boto3 EC2 client.
        inventory: Inventory dict, updated in place and saved.
        group: Our group identifier.
        tags: Mandatory tags.

    Returns:
        None.

    Raises:
        ConfigError: If a same-name group belongs to another group, or the
            emulator has no VPC/image.
        ClientError: If EC2 fails.

    Side Effects:
        Saves the inventory after each creation, and stores the
        ``ClientToken`` BEFORE requesting the instance so an interrupted
        run can be repeated without duplicating it.
    """
    vpc_id, image_id = cloud.mock_inputs(ec2)
    node = inventory["resources"].setdefault("node-01", {})
    node.update(type="ec2", expected_tags=tags)
    sg_name = f"fenix-sg-{group}"
    group_sg = _find_group(ec2, sg_name, group)
    if group_sg is None:
        group_sg = cloud.create_group(ec2, sg_name, vpc_id, tags)
    node["security_group_id"] = group_sg
    state.save_inventory(inventory)
    current = None
    if node.get("id"):
        current = cloud.instance_state(ec2, node["id"])
    if current == "terminated":
        node.pop("client_token", None)
    if current in (None, "terminated"):
        found = _find_instance(ec2, group, "node-01")
        if found is None:
            if not node.get("client_token"):
                node["client_token"] = f"fenix-{group}-{uuid.uuid4().hex[:12]}"
                state.save_inventory(inventory)
            found = cloud.create_instance(
                ec2, image_id, group_sg,
                {**tags, "Alias": "node-01", "Name": "node-01"},
                node["client_token"])
        node["id"] = found
        state.save_inventory(inventory)
    log.info("nodo listo", extra={"resource_id": "node-01",
                                  "action": "setup", "status": "ok"})


def _setup_bucket(s3, inventory, group, tags):
    """Create or reuse the bucket ``bucket-01`` and complete its tags.

    Args:
        s3: A Boto3 S3 client.
        inventory: Inventory dict, updated in place and saved.
        group: Our group identifier.
        tags: Mandatory tags.
    Returns:
        None.

    Raises:
        ConfigError: If the bucket exists and belongs to another group.
        ClientError: If S3 fails.

    Side Effects:
        Saves the inventory right after creating the bucket and BEFORE
        tagging it: if tagging fails, repeating the command finishes the
        job (a partial failure that must be demonstrated, not hidden).
    """
    name = f"fenix-{group}-bucket-01"
    current = {}
    if cloud.bucket_exists(s3, name):
        current = cloud.get_bucket_tags(s3, name)
        if current.get("GroupId", group) != group:
            raise ConfigError(f"El bucket {name} es de otro grupo")
    else:
        cloud.create_bucket(s3, name)
    inventory["resources"]["bucket-01"] = {
        "type": "s3", "id": name, "expected_tags": tags}
    state.save_inventory(inventory)
    cloud.put_tags(s3, name, {**current, **tags})
    log.info("bucket listo", extra={"resource_id": "bucket-01",
                                    "action": "setup", "status": "ok"})


def setup_demo(ec2=None, s3=None):
    """Create or reuse the lab resources of the group.

    Args:
        ec2: Optional EC2 client (tests).
        s3: Optional S3 client (tests).

    Returns:
        The inventory dict (also saved in ``data/inventory.json``).

    Raises:
        ConfigError: If a resource of another group is in the way.
        ClientError: If a Boto3 call fails.

    Side Effects:
        May create a security group, an instance and a bucket. Repeating
        it never creates duplicates.
    """
    ec2 = ec2 or aws.get_client("ec2")
    s3 = s3 or aws.get_client("s3")
    group = settings.group_id()
    inventory = state.load_inventory()
    inventory["group_id"] = group
    inventory.setdefault("resources", {})
    tags = expected_tags(group)
    _setup_node(ec2, inventory, group, tags)
    _setup_bucket(s3, inventory, group, tags)
    node_id = inventory["resources"]["node-01"]["id"]
    _wait_state(ec2, node_id, "running")
    return inventory


def inject_fault(alias, ec2=None, s3=None):
    """Provoke the scenario fault on a resource.

    Args:
        alias: ``node-01`` (stop the instance) or ``bucket-01`` (remove the
            ``Owner`` tag and add a foreign ``CostCenter`` tag).
        ec2: Optional EC2 client (tests).
        s3: Optional S3 client (tests).

    Returns:
        A short text describing the new state.

    Raises:
        ValidationError: If alias is not in the inventory.
        ClientError: If a Boto3 call fails.

    Side Effects:
        Changes a real (emulated) resource.
    """
    resource = state.load_inventory().get("resources", {}).get(alias)
    if resource is None:
        raise ValidationError(f"{alias} no está en el inventario")
    if resource["type"] == "ec2":
        ec2 = ec2 or aws.get_client("ec2")
        ec2.stop_instances(InstanceIds=[resource["id"]])
        final = _wait_state(ec2, resource["id"], "stopped")
        return f"{alias}: estado {final}"
    s3 = s3 or aws.get_client("s3")
    tags = cloud.get_bucket_tags(s3, resource["id"])
    tags.pop("Owner", None)
    tags.setdefault("CostCenter", "lab")
    cloud.put_tags(s3, resource["id"], tags)
    return f"{alias}: etiquetas ahora {sorted(tags)}"


def status(ec2=None, s3=None):
    """Query the REAL state of every inventoried resource through the API.

    Args:
        ec2: Optional EC2 client (tests).
        s3: Optional S3 client (tests).

    Returns:
        A dict alias -> info (instance state, or bucket tags).

    Raises:
        ClientError: If a Boto3 call fails.

    Notes:
        This is what separates "the log says it was repaired" from "the
        cloud confirms it is repaired".
    """
    ec2 = ec2 or aws.get_client("ec2")
    s3 = s3 or aws.get_client("s3")
    report = {}
    for alias, res in state.load_inventory().get("resources", {}).items():
        if res["type"] == "ec2":
            report[alias] = {"id": res.get("id"),
                             "state": cloud.instance_state(ec2, res["id"])}
        else:
            report[alias] = {"id": res["id"],
                             "tags": cloud.get_bucket_tags(s3, res["id"])}
    return report


def _empty_bucket(s3, name):
    """Delete every object of a bucket (paginated).

    Args:
        s3: A Boto3 S3 client.
        name: Bucket name.

    Returns:
        None.

    Raises:
        ConfigError: If ``delete_objects`` reports per-object errors.
        ClientError: If S3 fails.
    """
    token = None
    while True:
        args = {"Bucket": name}
        if token:
            args["ContinuationToken"] = token
        page = s3.list_objects_v2(**args)
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            result = s3.delete_objects(
                Bucket=name, Delete={"Objects": objects})
            if result.get("Errors"):
                raise ConfigError("no se pudieron borrar todos los objetos")
        token = page.get("NextContinuationToken")
        if not token:
            return


def cleanup(ec2=None, s3=None):
    """Delete ONLY the inventoried resources and empty the inventory.

    Args:
        ec2: Optional EC2 client (tests).
        s3: Optional S3 client (tests).

    Returns:
        None.

    Raises:
        ConfigError: If a bucket cannot be emptied.
        ClientError: For unexpected Boto3 errors.

    Side Effects:
        Terminates the instance, deletes the security group when no longer
        in use, empties and deletes the bucket, and clears the resources
        of ``data/inventory.json``.
    """
    ec2 = ec2 or aws.get_client("ec2")
    s3 = s3 or aws.get_client("s3")
    inventory = state.load_inventory()
    for alias, res in list(inventory.get("resources", {}).items()):
        if res["type"] == "ec2":
            if res.get("id"):
                ec2.terminate_instances(InstanceIds=[res["id"]])
                _wait_state(ec2, res["id"], "terminated")
            for _ in range(5):
                try:
                    ec2.delete_security_group(GroupId=res["security_group_id"])
                    break
                except ClientError as error:
                    if cloud.error_code(error) != "DependencyViolation":
                        raise
                    time.sleep(1)
        elif cloud.bucket_exists(s3, res["id"]):
            _empty_bucket(s3, res["id"])
            s3.delete_bucket(Bucket=res["id"])
        log.info("recurso eliminado", extra={"resource_id": alias,
                                             "action": "cleanup"})
    inventory["resources"] = {}
    state.save_inventory(inventory)
