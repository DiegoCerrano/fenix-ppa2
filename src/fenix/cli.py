"""Command line interface: ``python -m fenix.cli <command>``."""

import argparse
import json
import sys
import time
from pathlib import Path

import requests
from botocore.exceptions import BotoCoreError, ClientError

from fenix.config import settings
from fenix.services import demo, sqs, worker
from fenix.utils import crypto, logging_json
from fenix.utils.errors import FenixError
from fenix.utils.logging_json import get_logger

log = get_logger("cli")


def _show(data):
    """Print a JSON-serializable object in a readable way.

    Args:
        data: Any JSON-serializable object.

    Returns:
        None.

    Raises:
        TypeError: If data is not JSON-serializable.
    """
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_init_queues(args):
    """Create or reuse the DLQ and the main queue.

    Args:
        args: Parsed arguments (unused).

    Returns:
        None.

    Raises:
        QueueUnavailableError: If SQS fails.
    """
    _show(sqs.ensure_queues())


def cmd_setup_demo(args):
    """Create or reuse the lab resources.

    Args:
        args: Parsed arguments (unused).
    Returns:
        None.

    Raises:
        ConfigError: If a resource of another group is in the way.
        ClientError: If a Boto3 call fails.
    """
    _show(demo.setup_demo())


def cmd_fault(args):
    """Provoke the scenario fault on a resource.

    Args:
        args: Parsed arguments; ``args.alias`` is ``node-01`` or
            ``bucket-01``.

    Returns:
        None.

    Raises:
        ValidationError: If the alias is unknown.
        ClientError: If a Boto3 call fails.
    """
    print(demo.inject_fault(args.alias))


def cmd_status(args):
    """Show the real state of resources and the queue counters.

    Args:
        args: Parsed arguments (unused).

    Returns:
        None.

    Raises:
        QueueUnavailableError: If SQS fails.
        ClientError: If a Boto3 call fails.
    """
    _show({"resources": demo.status(), "queues": sqs.stats()})


def cmd_worker(args):
    """Run the worker loop.

    Args:
        args: Parsed arguments; ``args.once`` limits it to one poll.

    Returns:
        None.

    Raises:
        KeyboardInterrupt: On Ctrl+C (handled by ``main``).
    """
    worker.run(once=args.once)


def cmd_dlq(args):
    """Show one message of the DLQ without deleting it.

    Args:
        args: Parsed arguments (unused).

    Returns:
        None.

    Raises:
        QueueUnavailableError: If SQS fails.
    """
    messages = sqs.peek_dlq()
    if not messages:
        print("La DLQ no tiene mensajes visibles (todavía).")
    for item in messages:
        _show({"MessageId": item["MessageId"], "Body": item["Body"],
               "Attributes": item.get("Attributes", {})})


def cmd_inject_raw(args):
    """Put raw text straight into SQS (skips the receiver).

    Args:
        args: Parsed arguments; ``args.text`` is the message body.

    Returns:
        None.

    Raises:
        QueueUnavailableError: If SQS fails.
    """
    print("MessageId:", sqs.send_raw(args.text))


def cmd_cleanup(args):
    """Delete the inventoried resources (and optionally the queues).

    Args:
        args: Parsed arguments; ``args.queues`` also deletes the queues.

    Returns:
        None.

    Raises:
        ClientError: If a Boto3 call fails.
        QueueUnavailableError: If deleting the queues fails.
    """
    demo.cleanup()
    if args.queues:
        sqs.delete_queues()
    print("Limpieza terminada.")


def cmd_send(args):
    """Sign an event file and POST it to the receiver.
    Args:
        args: Parsed arguments: ``file`` plus the optional overrides
            ``event_id`` and ``failures`` and the test switches
            ``no_sig``, ``bad_sig``, ``old`` and ``tamper``.

    Returns:
        None. Prints the HTTP status, the elapsed time and the answer.

    Raises:
        FenixError: If the file cannot be modified as JSON or the receiver
            cannot be reached.
        ConfigError: If ``WEBHOOK_SECRET`` is missing or too short.

    Notes:
        It signs the very same bytes it sends, except with ``--tamper``,
        which changes the body AFTER signing.
    """
    raw = Path(args.file).read_bytes()
    if args.event_id or args.failures is not None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise FenixError("el archivo no es JSON: no puedo modificarlo")
        if args.event_id:
            data["event_id"] = args.event_id
        if args.failures is not None:
            data["simulate_failures"] = args.failures
        raw = json.dumps(data).encode()
    stamp = int(time.time()) - (3600 if args.old else 0)
    signature = crypto.sign(settings.webhook_secret(), raw, stamp)
    if args.bad_sig:
        last = signature[-1]
        signature = signature[:-1] + ("0" if last != "0" else "1")
    if args.tamper:
        raw = raw + b" "  # the bytes change AFTER signing
    headers = {"Content-Type": "application/json"}
    if not args.no_sig:
        headers["X-Webhook-Signature"] = signature
    url = settings.env("WEBHOOK_URL", "http://127.0.0.1:8000/webhook")
    started = time.perf_counter()
    try:
        response = requests.post(url, data=raw, headers=headers,
                                 timeout=(2, 5))
    except requests.RequestException as error:
        raise FenixError(
            f"no pude hablar con el receptor ({type(error).__name__})"
        ) from None
    elapsed = (time.perf_counter() - started) * 1000
    print(f"HTTP {response.status_code} ({elapsed:.0f} ms) {response.text}")


def build_parser():
    """Build the argument parser with all sub-commands.

    Args:
        None.
    Returns:
        A configured ``argparse.ArgumentParser``.

    Raises:
        None.
    """
    parser = argparse.ArgumentParser(prog="fenix")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-queues").set_defaults(func=cmd_init_queues)
    sub.add_parser("setup-demo").set_defaults(func=cmd_setup_demo)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("dlq").set_defaults(func=cmd_dlq)
    fault = sub.add_parser("fault")
    fault.add_argument("alias")
    fault.set_defaults(func=cmd_fault)
    work = sub.add_parser("worker")
    work.add_argument("--once", action="store_true")
    work.set_defaults(func=cmd_worker)
    raw = sub.add_parser("inject-raw")
    raw.add_argument("text")
    raw.set_defaults(func=cmd_inject_raw)
    clean = sub.add_parser("cleanup")
    clean.add_argument("--queues", action="store_true")
    clean.set_defaults(func=cmd_cleanup)
    send = sub.add_parser("send")
    send.add_argument("file")
    send.add_argument("--event-id")
    send.add_argument("--failures", type=int)
    send.add_argument("--no-sig", action="store_true")
    send.add_argument("--bad-sig", action="store_true")
    send.add_argument("--old", action="store_true")
    send.add_argument("--tamper", action="store_true")
    send.set_defaults(func=cmd_send)
    return parser


def main(argv=None):
    """Entry point: parse, set up logging, run, and always stop logging.

    Args:
        argv: Optional list of arguments (defaults to ``sys.argv``).

    Returns:
        Process exit code: 0 on success or Ctrl+C, 1 after a fatal error
        (which is logged as CRITICAL first: a controlled shutdown).

    Raises:
        SystemExit: Raised by ``argparse`` on invalid arguments.

    Side Effects:
        Starts and, in a ``finally`` block, stops the log listener. It
        never acknowledges messages or destroys resources from here.
    """
    args = build_parser().parse_args(argv)
    name = "worker" if args.command == "worker" else f"cli-{args.command}"
    logging_json.setup_logging(name)
    try:
        args.func(args)
        return 0
    except KeyboardInterrupt:
        log.info("detenido por el usuario",
                 extra={"action": "shutdown", "status": "stopped"})
        return 0
    except FenixError as error:
        log.critical(str(error), extra={"action": args.command,
                                        "status": "fatal"})
        return 1
    except (ClientError, BotoCoreError) as error:
        log.critical(f"Boto3: {type(error).__name__}",
                     extra={"action": args.command, "status": "fatal"})
        return 1
    finally:
        logging_json.shutdown_logging()


if __name__ == "__main__":
    sys.exit(main())
