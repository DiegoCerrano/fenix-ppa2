"""Automatic tests for Fenix (F03-F09, F11, F12, F14, F15 and F16).

They also cover the AWS boundary in ``fenix.config.aws``: fake keys,
loopback endpoint and known services only.

Run them with ``python -m unittest discover -s tests -v`` (pytest works
too). They do not need LocalStack: EC2 and S3 are replaced by fakes.
"""

import gzip
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from botocore.exceptions import ClientError

from fenix.config import aws
from fenix.services import events, receiver, remediation, sqs, worker
from fenix.utils import crypto, logging_json, state
from fenix.utils.errors import (ConfigError, ConflictError,
                                NotifyTransientError,
                                QueueUnavailableError, SignatureError,
                                SimulatedFailure, ValidationError)

SECRET = "secreto-de-prueba-muy-largo-123"
TAGS = {"ManagedBy": "ppa2", "Project": "fenix",
        "GroupId": "grupo01", "Owner": "grupo01"}
INVENTORY = {"group_id": "grupo01", "resources": {
    "node-01": {"type": "ec2", "id": "i-1", "security_group_id": "sg-1",
                "expected_tags": TAGS},
    "bucket-01": {"type": "s3", "id": "fenix-grupo01-bucket-01",
                  "expected_tags": TAGS}}}


def make_event(**changes):
    """Build a valid event and apply overrides.

    Args:
        **changes: Fields to replace in the base event.

    Returns:
        A new event dict.

    Raises:
        None.
    """
    event = {"schema_version": 1, "event_id": "evt-node-001",
             "event_type": "instance.stopped", "resource_id": "node-01",
             "occurred_at": "2026-11-10T18:00:00Z", "simulate_failures": 0}
    event.update(changes)
    return event


class FakeEC2:
    """In-memory stand-in for the EC2 client (one instance, ``i-1``)."""

    def __init__(self, current="stopped"):
        """Create the fake.

        Args:
            current: Initial instance state name.

        Returns:
            None.

        Raises:
            None.
        """
        self.current, self.starts = current, 0

    def describe_instances(self, **kwargs):
        """Return the current state in the shape Boto3 uses.

        Args:
            **kwargs: Ignored (``InstanceIds``, ``Filters``...).

        Returns:
            A dict with one reservation and one instance.

        Raises:
            None.
        """
        return {"Reservations": [{"Instances": [
            {"InstanceId": "i-1", "State": {"Name": self.current}}]}]}

    def start_instances(self, InstanceIds):
        """Start the instance and count the call.

        Args:
            InstanceIds: Ignored.

        Returns:
            None.

        Raises:
            None.
        """
        self.starts += 1
        self.current = "running"


class FakeS3:
    """In-memory stand-in for the S3 client (bucket tagging only)."""

    def __init__(self, tags):
        """Create the fake.

        Args:
            tags: Initial tag dict.

        Returns:
            None.
        Raises:
            None.
        """
        self.tags, self.puts = dict(tags), 0

    def get_bucket_tagging(self, Bucket):
        """Return the tag set, or fail like S3 when it is empty.

        Args:
            Bucket: Ignored.

        Returns:
            A dict with a ``TagSet`` list.

        Raises:
            ClientError: ``NoSuchTagSet`` when there are no tags.
        """
        if not self.tags:
            raise ClientError({"Error": {"Code": "NoSuchTagSet"}}, "Get")
        return {"TagSet": [{"Key": k, "Value": v}
                           for k, v in self.tags.items()]}

    def put_bucket_tagging(self, Bucket, Tagging):
        """Replace the whole tag set and count the call.

        Args:
            Bucket: Ignored.
            Tagging: Dict with the new ``TagSet`` list.

        Returns:
            None.

        Raises:
            None.
        """
        self.puts += 1
        self.tags = {t["Key"]: t["Value"] for t in Tagging["TagSet"]}


class LabCase(unittest.TestCase):
    """Isolated lab: temp folders, own secret and no real sleeping."""

    def setUp(self):
        """Prepare the temp folders, environment and saved inventory.

        Args:
            None.

        Returns:
            None.

        Raises:
            None.
        """
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "FENIX_DATA_DIR": str(Path(folder.name) / "data"),
            "FENIX_LOG_DIR": str(Path(folder.name) / "logs"),
            "WEBHOOK_SECRET": SECRET, "GROUP_ID": "grupo01",
            "FENIX_CRASH_AFTER_REPAIR": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch("time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        state.save_inventory(INVENTORY)
        self.tmp = Path(folder.name)


class TestValidation(LabCase):
    """F06 (422 contract errors) and F07 (alien alias, extra keys)."""

    def test_evento_correcto(self):
        """A valid event comes back unchanged."""
        self.assertEqual(events.validate(make_event(), INVENTORY),
                         make_event())

    def test_rechazos(self):
        """Every kind of invalid event raises ValidationError."""
        malos = [
            ["lista", "no", "objeto"],
            make_event(hack="extra"),
            {k: v for k, v in make_event().items() if k != "event_id"},
            make_event(simulate_failures=True),
            make_event(simulate_failures=6),
            make_event(event_type="otra.cosa"),
            make_event(resource_id="node-99"),
            make_event(event_type="bucket.tags_missing"),
            make_event(occurred_at="2026-11-10T18:00:00"),
            make_event(schema_version=True),
            make_event(event_id="MAYUSCULAS"),
        ]
        for evento in malos:
            with self.subTest(evento=evento):
                with self.assertRaises(ValidationError):
                    events.validate(evento, INVENTORY)


class TestCrypto(unittest.TestCase):
    """F04 (bad signatures) and F05 (bytes changed after signing)."""

    body = b'{"a": 1}'

    def test_firma_valida(self):
        """A fresh signature over the same bytes verifies."""
        crypto.verify(SECRET, crypto.sign(SECRET, self.body), self.body)

    def test_rechazos(self):
        """Absent, altered, stale, duplicated and tampered all fail."""
        good = crypto.sign(SECRET, self.body)
        old = crypto.sign(SECRET, self.body, int(time.time()) - 3600)
        casos = {
            "ausente": (None, self.body),
            "vacia": ("", self.body),
            "alterada": (good[:-1] + ("0" if good[-1] != "0" else "1"),
                         self.body),
            "vieja": (old, self.body),
            "bytes_distintos": (good, self.body + b" "),
            "duplicada": (good + "," + good.split(",")[1], self.body),
            "sin_v1": ("t=123", self.body),
        }
        for nombre, (header, body) in casos.items():
            with self.subTest(caso=nombre):
                with self.assertRaises(SignatureError):
                    crypto.verify(SECRET, header, body)


class TestReceiver(LabCase):
    """F04-F07 and F13 through the pure ``handle_webhook`` function."""

    def post(self, raw, header="auto"):
        """Call ``handle_webhook`` signing the body when asked.

        Args:
            raw: Body bytes.
            header: ``"auto"`` to sign, or an explicit header (or None).

        Returns:
            The ``(status, payload)`` tuple of the receiver.

        Raises:
            None.
        """
        if header == "auto":
            header = crypto.sign(SECRET, raw)
        return receiver.handle_webhook(raw, header)

    def test_202_publica_una_vez(self):
        """A valid signed event is published exactly once."""
        raw = json.dumps(make_event()).encode()
        with mock.patch.object(sqs, "publish", return_value="m-1") as pub:
            code, body = self.post(raw)
        self.assertEqual((code, body["message_id"]), (202, "m-1"))
        pub.assert_called_once()

    def test_rechazos_sin_publicar(self):
        """401, 400 and 422 cases never reach SQS."""
        casos = [
            (b"{}", None, 401),
            (b"{}", "t=1,v1=" + "0" * 64, 401),
            (b"{roto", "auto", 400),
            (b"\xff\xfe", "auto", 400),
            (b"[1, 2]", "auto", 422),
            (json.dumps(make_event(resource_id="node-99")).encode(),
             "auto", 422),
            (json.dumps(make_event(hack=1)).encode(), "auto", 422),
        ]
        for raw, header, esperado in casos:
            with self.subTest(raw=raw[:15], esperado=esperado):
                with mock.patch.object(sqs, "publish") as pub:
                    code, _ = self.post(raw, header)
                self.assertEqual(code, esperado)
                pub.assert_not_called()

    def test_503_si_sqs_cae(self):
        """F13: when SQS is down the answer is 503, never a fake 202."""
        raw = json.dumps(make_event()).encode()
        with mock.patch.object(sqs, "publish",
                               side_effect=QueueUnavailableError("x")):
            code, _ = self.post(raw)
        self.assertEqual(code, 503)


class TestSqsHelpers(unittest.TestCase):
    """Backoff of 2, 4, 8 seconds with a cap of 30."""

    def test_backoff(self):
        """The visibility delay follows min(30, 2 ** receive_count)."""
        client = mock.Mock()
        with mock.patch.object(sqs, "queue_url", return_value="u"):
            delays = [sqs.retry_later("rh", n, client=client)
                      for n in (1, 2, 3, 10)]
        self.assertEqual(delays, [2, 4, 8, 30])
        self.assertEqual(
            client.change_message_visibility.call_args.kwargs
            ["VisibilityTimeout"], 30)


class TestAwsConfig(unittest.TestCase):
    """The AWS boundary: fake keys, loopback only and known services."""

    SAFE = {"AWS_ENDPOINT_URL": "http://localhost:4566",
            "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-east-1"}

    def test_settings_no_carga_boto3(self):
        """Reading the environment never imports Boto3."""
        code = ("import sys; from fenix.config import settings; "
                "print('boto3' in sys.modules)")
        done = subprocess.run([sys.executable, "-c", code], check=True,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout.strip(), "False")

    def test_rechazos_antes_de_crear_el_cliente(self):
        """Unknown service, foreign endpoint or real key: no client at all."""
        casos = [
            ("dynamodb", {}),
            ("sqs", {"AWS_ENDPOINT_URL": "http://example.com:4566"}),
            ("sqs", {"AWS_ENDPOINT_URL": "http://localhost:4567"}),
            ("sqs", {"AWS_ACCESS_KEY_ID": "AKIAREAL"}),
            ("s3", {"AWS_ACCESS_KEY_ID": "ASIAREAL"}),
        ]
        for service, cambios in casos:
            with self.subTest(service=service, cambios=cambios):
                with mock.patch.dict(os.environ, {**self.SAFE, **cambios}), \
                        mock.patch("boto3.client") as client:
                    with self.assertRaises(ConfigError):
                        aws.get_client(service)
                client.assert_not_called()

    def test_clientes_apuntan_a_localstack(self):
        """Each client gets LocalStack, the region and bounded timeouts."""
        lectura = {"sqs": 25, "s3": 10, "ec2": 10}
        with mock.patch.dict(os.environ, self.SAFE):
            for service, segundos in lectura.items():
                with self.subTest(service=service):
                    client = aws.get_client(service)
                    config = client.meta.config
                    self.assertEqual(client.meta.endpoint_url,
                                     "http://localhost:4566")
                    self.assertEqual(client.meta.region_name, "us-east-1")
                    self.assertEqual(
                        (config.connect_timeout, config.read_timeout),
                        (2, segundos))
            s3_config = aws.get_client("s3").meta.config.s3
        self.assertEqual(s3_config["addressing_style"], "path")


class TestWorker(LabCase):
    """F03, F08, F09, F11, F12, F14 and F15 with fake EC2 and S3."""

    def setUp(self):
        """Install the fake clients on top of the isolated lab.

        Args:
            None.

        Returns:
            None.

        Raises:
            None.
        """
        super().setUp()
        self.ec2, self.s3 = FakeEC2(), FakeS3({})
        clients = {"ec2": self.ec2, "s3": self.s3}
        patcher = mock.patch("fenix.config.aws.get_client",
                             side_effect=lambda name: clients[name])
        patcher.start()
        self.addCleanup(patcher.stop)

    def ledger(self, event_id="evt-node-001"):
        """Read one record of the ledger.
        Args:
            event_id: Event to look up.

        Returns:
            The ledger record dict.

        Raises:
            KeyError: If the event is not in the ledger.
        """
        return state.load_ledger()["events"][event_id]

    def test_f09_fallas_simuladas_y_luego_exito(self):
        """F09: two simulated failures, then repair on the third run."""
        event = make_event(simulate_failures=2)
        with mock.patch("fenix.services.notifications.notify"):
            for _ in range(2):
                with self.assertRaises(SimulatedFailure):
                    remediation.handle(event)
            self.assertEqual(self.ec2.starts, 0)
            self.assertEqual(remediation.handle(event), "done")
        self.assertEqual((self.ec2.starts, self.ledger()["attempts"]), (1, 3))

    def test_f11_repetir_no_repara_ni_avisa(self):
        """F11: a completed event does not repair or notify again."""
        with mock.patch("fenix.services.notifications.notify") as notify:
            self.assertEqual(remediation.handle(make_event()), "done")
            self.assertEqual(remediation.handle(make_event()),
                             "already_done")
        self.assertEqual((self.ec2.starts, notify.call_count), (1, 1))

    def test_f12_f14_aviso_pendiente(self):
        """F12/F14: a failing notice is retried without repairing again."""
        fallo = NotifyTransientError("429", retry_after=5)
        with mock.patch("fenix.services.notifications.notify",
                        side_effect=[fallo, None]) as notify:
            with self.assertRaises(NotifyTransientError):
                remediation.handle(make_event())
            guardado = self.ledger()
            self.assertTrue(guardado["repair_done"])
            self.assertFalse(guardado["notified"])
            self.assertEqual(remediation.handle(make_event()), "done")
        self.assertEqual((self.ec2.starts, notify.call_count), (1, 2))

    def test_f03_bucket_preserva_etiquetas_ajenas(self):
        """F03: missing tags are restored and foreign ones are kept."""
        self.s3.tags = {"CostCenter": "lab", "Project": "fenix"}
        event = make_event(event_id="evt-bucket-001",
                           event_type="bucket.tags_missing",
                           resource_id="bucket-01")
        with mock.patch("fenix.services.notifications.notify"):
            remediation.handle(event)
        self.assertEqual(self.s3.tags, {**TAGS, "CostCenter": "lab"})

    def test_f15_conflicto_no_toca_el_primer_registro(self):
        """F15: same id with other content is a conflict, nothing moves."""
        with mock.patch("fenix.services.notifications.notify"):
            remediation.handle(make_event())
        antes = self.ledger()
        with self.assertRaises(ConflictError):
            remediation.handle(make_event(simulate_failures=1))
        self.assertEqual(self.ledger(), antes)
        self.assertEqual(self.ec2.starts, 1)

    def message(self, body, count=1):
        """Build an SQS message dict.

        Args:
            body: Message body text.
            count: Value of ``ApproximateReceiveCount``.

        Returns:
            A message dict like the one ``receive_message`` returns.

        Raises:
            None.
        """
        return {"MessageId": "m-1", "ReceiptHandle": "rh-1", "Body": body,
                "Attributes": {"ApproximateReceiveCount": str(count)}}

    def test_f08_mensaje_corrupto_no_tumba_ni_se_confirma(self):
        """F08: a corrupt body is kept, not acknowledged, worker survives."""
        with mock.patch.object(sqs, "ack") as ack, \
                mock.patch.object(sqs, "retry_later", return_value=2) as rl:
            self.assertEqual(
                worker.process_message(self.message("esto no es json {")),
                "failed")
        ack.assert_not_called()
        rl.assert_called_once()

    def test_ack_solo_despues_de_avisar(self):
        """The ACK happens only after repair AND notification succeeded."""
        body = json.dumps(make_event())
        fallo = NotifyTransientError("429", retry_after=20)
        with mock.patch.object(sqs, "ack") as ack, \
                mock.patch.object(sqs, "retry_later", return_value=20) as rl, \
                mock.patch("fenix.services.notifications.notify",
                           side_effect=[fallo, None]):
            self.assertEqual(worker.process_message(self.message(body)),
                             "failed")
            ack.assert_not_called()
            self.assertEqual(rl.call_args.args[2], 20)
            self.assertEqual(worker.process_message(self.message(body, 2)),
                             "done")
            ack.assert_called_once_with("rh-1")
        self.assertEqual(self.ec2.starts, 1)


class TestLogging(unittest.TestCase):
    """F16: rotation, readable gzip, valid JSON and no secrets."""

    def test_rotacion_gzip_json_sin_secretos(self):
        """Rotated gzip files hold valid JSON lines with no secret inside."""
        secreto = "https://hooks.slack.com/services/T000/B000/XXXXSECRETO"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "t.log"
            handler = logging_json.build_file_handler(
                path, max_bytes=400, backups=50)
            logger = logging.getLogger("fenix.test16")
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.addHandler(handler)
            niveles = [logging.DEBUG, logging.INFO, logging.WARNING,
                       logging.ERROR, logging.CRITICAL]
            for i in range(30):
                logger.log(niveles[i % 5], f"linea {i} url {secreto}",
                           extra={"event_id": "evt-log-001", "attempt": i})
            handler.close()
            logger.removeHandler(handler)
            comprimidos = sorted(Path(folder).glob("t.log.*.gz"))
            self.assertTrue(comprimidos, "no hubo rotación")
            vistos = set()
            for archivo in comprimidos + [path]:
                abrir = gzip.open if archivo.suffix == ".gz" else open
                with abrir(archivo, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        entry = json.loads(line)
                        self.assertTrue(entry["timestamp"].endswith("Z"))
                        self.assertNotIn("XXXXSECRETO", line)
                        self.assertIsNone(entry["message_id"])
                        self.assertEqual(entry["event_id"], "evt-log-001")
                        vistos.add(entry["level"])
            self.assertEqual(vistos, {"DEBUG", "INFO", "WARNING",
                                      "ERROR", "CRITICAL"})


if __name__ == "__main__":
    unittest.main()
