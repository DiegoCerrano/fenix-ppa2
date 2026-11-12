#!/usr/bin/env python3
"""Start-of-session check: is the whole Fenix stack in the expected state?"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

OK, FAIL, WARN, SKIP = "OK", "FAIL", "WARN", "SKIP"
ROOT = Path(__file__).resolve().parent.parent


def http_json(url):
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def git(*args):
    try:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def check_python():
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info >= (3, 11):
        return OK, f"Python {version}"
    return FAIL, f"Python {version}: se necesita 3.11 o superior"


def check_venv():
    if sys.prefix != sys.base_prefix:
        return OK, f"venv activo ({Path(sys.prefix).name})"
    return FAIL, "activá el entorno: source .venv/bin/activate"


def check_package():
    try:
        import fenix  # noqa: F401
    except ImportError:
        return FAIL, "no se importa fenix: python -m pip install -e ."
    return OK, "paquete fenix importable"


def read_env():
    values = {}
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def check_env():
    if not (ROOT / ".env").is_file():
        return FAIL, "falta .env: cp .env.example .env"
    values = read_env()
    if len(values.get("WEBHOOK_SECRET", "")) < 16:
        return FAIL, "WEBHOOK_SECRET vacío o corto (mínimo 16 caracteres)"
    key = values.get("AWS_ACCESS_KEY_ID", "test")
    if key.startswith(("AKIA", "ASIA")):
        return FAIL, "AWS_ACCESS_KEY_ID parece una clave REAL"
    return OK, ".env presente, secreto cargado (no se muestra)"


def check_notify():
    values = read_env()
    provider = values.get("NOTIFY_PROVIDER", "console").lower()
    if provider == "console":
        return OK, "NOTIFY_PROVIDER=console (modo desarrollo)"
    if provider == "slack":
        url = values.get("SLACK_WEBHOOK_URL", "")
        if url.startswith("https://hooks.slack.com/"):
            return OK, "NOTIFY_PROVIDER=slack, webhook cargado (oculto)"
        return FAIL, "provider slack pero SLACK_WEBHOOK_URL no es válida"
    return FAIL, f"NOTIFY_PROVIDER desconocido: {provider}"


def check_localstack():
    try:
        health = http_json("http://localhost:4566/_localstack/health")
    except (OSError, ValueError):
        return FAIL, "LocalStack no responde: docker compose up -d"
    services = health.get("services", {})
    bad = [
        n
        for n in ("sqs", "s3", "ec2")
        if services.get(n) not in ("available", "running")
    ]
    if bad:
        return FAIL, f"servicios no disponibles: {bad}"
    return OK, "sqs, s3 y ec2 disponibles"


def aws_client(service):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        return None
    options = {
        "connect_timeout": 2,
        "read_timeout": 8,
        "retries": {"max_attempts": 1},
    }
    if service == "s3":
        options["s3"] = {"addressing_style": "path"}
    return boto3.client(
        service,
        endpoint_url="http://localhost:4566",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(**options),
    )


def check_queues():
    sqs = aws_client("sqs")
    if sqs is None:
        return SKIP, "boto3 todavía no está instalado"
    group = read_env().get("GROUP_ID", "grupo01")
    existed = (ROOT / "data" / "queues.json").is_file()
    try:
        url = sqs.get_queue_url(QueueName=f"fenix-events-{group}")["QueueUrl"]
        sqs.get_queue_url(QueueName=f"fenix-dlq-{group}")
    except Exception as error:
        code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
        if "NonExistent" not in code:
            return SKIP, "no pude consultar SQS (¿LocalStack caído?)"
        if existed:
            return FAIL, (
                "las colas existían y ya no están (LocalStack perdió el estado)"
            )
        return SKIP, "no existen aún (normal hasta el Paso 11)"
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])[
        "Attributes"
    ]
    policy = json.loads(attrs.get("RedrivePolicy", "{}"))
    if str(policy.get("maxReceiveCount")) != "3":
        return FAIL, "RedrivePolicy ausente: python -m fenix.cli init-queues"
    waiting = attrs.get("ApproximateNumberOfMessages", "?")
    hidden = attrs.get("ApproximateNumberOfMessagesNotVisible", "?")
    return OK, (
        f"cola y DLQ listas (maxReceiveCount=3); mensajes={waiting} ocultos={hidden}"
    )


def check_inventory():
    path = ROOT / "data" / "inventory.json"
    if not path.is_file():
        return SKIP, "sin inventario aún (normal hasta el Paso 12)"
    ec2, s3 = aws_client("ec2"), aws_client("s3")
    if ec2 is None or s3 is None:
        return SKIP, "boto3 todavía no está instalado"
    inventory = json.loads(path.read_text(encoding="utf-8"))
    parts, worst = [], OK
    try:
        for alias, res in inventory.get("resources", {}).items():
            if res["type"] == "ec2":
                found = ec2.describe_instances(InstanceIds=[res["id"]])
                state_name = found["Reservations"][0]["Instances"][0]["State"][
                    "Name"
                ]
                parts.append(f"{alias}={state_name}")
            else:
                tags = s3.get_bucket_tagging(Bucket=res["id"])["TagSet"]
                keys = {t["Key"] for t in tags}
                missing = set(res["expected_tags"]) - keys
                parts.append(
                    f"{alias}="
                    + (
                        "tags completos"
                        if not missing
                        else f"faltan {sorted(missing)}"
                    )
                )
                if missing:
                    worst = WARN
    except Exception:
        return FAIL, (
            "el inventario no coincide con LocalStack (¿perdió el estado?)"
        )
    return worst, ", ".join(parts) or "inventario vacío"


def check_ledger():
    path = ROOT / "data" / "events.json"
    if not path.is_file():
        return SKIP, "sin eventos todavía (normal hasta el Paso 14)"
    try:
        events = json.loads(path.read_text(encoding="utf-8"))["events"]
    except (ValueError, KeyError):
        return FAIL, "data/events.json está corrupto"
    counts = {}
    for record in events.values():
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    return OK, f"{len(events)} evento(s): {counts}"


def check_receiver():
    try:
        http_json("http://127.0.0.1:8000/health")
    except (OSError, ValueError):
        return WARN, "receptor apagado (normal si todavía no lo levantaste)"
    return OK, "receptor respondiendo en :8000"


def check_git():
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return WARN, "no es un repositorio git (git init)"
    dirty = git("status", "--porcelain") or ""
    changes = len([line for line in dirty.splitlines() if line.strip()])
    last = git("log", "-1", "--format=%h %s") or "sin commits"
    remote = "con remoto" if git("remote") else "SIN remoto"
    status = OK if changes == 0 and remote == "con remoto" else WARN
    return (
        status,
        f"rama {branch}, {changes} cambio(s) sin commit, {remote}; último: {last}",
    )


def main():
    checks = [
        ("python", check_python),
        ("venv", check_venv),
        ("paquete", check_package),
        (".env", check_env),
        ("aviso", check_notify),
        ("localstack", check_localstack),
        ("colas", check_queues),
        ("inventario", check_inventory),
        ("ledger", check_ledger),
        ("receptor", check_receiver),
        ("git", check_git),
    ]
    failed = 0
    print("== Chequeo del stack Fénix ==")
    for name, check in checks:
        try:
            status, detail = check()
        except Exception as error:
            status, detail = FAIL, f"el chequeo falló ({type(error).__name__})"
        failed += status == FAIL
        print(f"[{status}] {name:<11} {detail}")

    print(
        "\nResultado:",
        "TODO BIEN"
        if not failed
        else f"{failed} chequeo(s) en FAIL: corregí de arriba hacia abajo",
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
