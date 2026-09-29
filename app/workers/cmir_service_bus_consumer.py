"""Standalone Service Bus consumer that forwards inbound CMIR emails to the API.

Runs as its own process (not through `app.workers.loop`'s claim/execute/settle
job queue): it holds a session-scoped receiver open indefinitely and forwards
each message directly to `PROCESS_EMAIL_PATH` over HTTP, retrying the whole
receive loop on any error until SIGTERM/SIGINT is received.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from typing import Any

import httpx

from app.core.config import ServiceBusConfig, get_settings

LOGGER = logging.getLogger(__name__)
shutdown_requested = False
PROCESS_EMAIL_PATH = "/api/v1/internal/process-email"


def _handle_shutdown(signum: int, frame: Any) -> None:
    """Flip the module-level shutdown flag so `run`'s receive loop exits after the current message."""
    global shutdown_requested
    shutdown_requested = True
    LOGGER.info("Shutdown requested by signal %s", signum)


def _message_body(message: Any) -> dict[str, Any]:
    """Decode a Service Bus message body to its JSON payload, tolerating str/bytes/chunked bodies."""
    raw_body = getattr(message, "body", None)
    if raw_body is None:
        body = str(message)
    elif isinstance(raw_body, bytes):
        body = raw_body.decode("utf-8")
    elif isinstance(raw_body, str):
        body = raw_body
    else:
        body = b"".join(raw_body).decode("utf-8")
    return json.loads(body)


def _process_email_payload(message: Any) -> dict[str, Any]:
    """Translate a queue message's raw payload into the internal process-email request body."""
    payload = _message_body(message)
    email_id = payload["email_id"]
    queue_message_id = str(getattr(message, "message_id", None) or email_id)
    imap_id = payload.get("source_imap_id") or payload.get("imap_id")
    return {
        "batch_id": payload["batch_id"],
        "email_id": email_id,
        "queue_message_id": queue_message_id,
        "email": {
            "email_id": email_id,
            "imap_id": imap_id or email_id,
            "sender": payload["sender"],
            "subject": payload["subject"],
            "body": payload["body"],
            "source_message_id": payload.get("source_message_id"),
            "mark_read": imap_id is not None,
        },
    }


def _process_email_url(base_url: str) -> str:
    """Join the configured agent API base URL with the process-email path."""
    return f"{base_url.rstrip('/')}{PROCESS_EMAIL_PATH}"


def _forward_to_api(client: httpx.Client, process_email_url: str, payload: dict[str, Any]) -> None:
    """POST the payload to the process-email endpoint, raising on a non-2xx response."""
    response = client.post(process_email_url, json=payload)
    response.raise_for_status()


def _process_message(receiver: Any, message: Any, client: httpx.Client, process_email_url: str) -> None:
    """Forward one message to the API and settle it: complete on success, abandon and re-raise on failure.

    Abandoning (rather than dead-lettering) lets Service Bus redeliver the
    message so a transient API outage self-heals once `run`'s outer retry
    loop reconnects; there is no attempt limit at this layer.
    """
    request_payload = _process_email_payload(message)
    email_id = request_payload["email_id"]
    queue_message_id = request_payload["queue_message_id"]
    try:
        _forward_to_api(client, process_email_url, request_payload)
        receiver.complete_message(message)
        LOGGER.info("Completed Service Bus message_id=%s email_id=%s", queue_message_id, email_id)
    except Exception:
        receiver.abandon_message(message)
        LOGGER.exception("Abandoned Service Bus message_id=%s email_id=%s", queue_message_id, email_id)
        raise


def run() -> None:
    """Run the consumer until SIGTERM/SIGINT: receive, forward, and settle messages forever.

    Authenticates with a connection string if configured, otherwise
    `DefaultAzureCredential`. Holds one session-scoped receiver with an
    `AutoLockRenewer` open at a time and processes messages serially; unlike
    `app.workers.loop` there is no concurrent claim/execute/settle pool. Any error
    other than a shutdown request reopens the receiver after a 5-second pause, so a
    dropped connection is retried indefinitely rather than crashing the process.
    """
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import AutoLockRenewer, ServiceBusClient

    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    config = ServiceBusConfig.from_settings(get_settings())
    if config.connection_string:
        LOGGER.info("Using Service Bus Connection String")

        client = ServiceBusClient.from_connection_string(conn_str=config.connection_string)

    # ------------------------------------------
    # Production: Managed Identity
    # ------------------------------------------
    else:
        LOGGER.info("Using Azure Default Credential")

        credential = DefaultAzureCredential()

        client = ServiceBusClient(
            fully_qualified_namespace=config.fully_qualified_namespace,
            credential=credential,
        )

    process_email_url = _process_email_url(config.agent_api_base_url)
    with client, httpx.Client(timeout=config.agent_api_timeout_seconds) as http_client:
        while not shutdown_requested:
            try:
                with (
                    AutoLockRenewer() as renewer,
                    client.get_queue_receiver(
                        queue_name=config.queue_name,
                        session_id=config.session_id,
                        max_wait_time=config.max_wait_time_seconds,
                    ) as receiver,
                ):
                    renewer.register(
                        receiver,
                        receiver.session,
                        max_lock_renewal_duration=config.lock_renew_seconds,
                    )
                    LOGGER.info(
                        "Listening on Service Bus session %s and forwarding to %s",
                        config.session_id,
                        process_email_url,
                    )
                    for message in receiver:
                        _process_message(receiver, message, http_client, process_email_url)
                        if shutdown_requested:
                            break
            except Exception as exc:  # noqa: BLE001
                if shutdown_requested:
                    break
                LOGGER.warning("Receiver loop error; retrying in 5 seconds: %s", exc)
                time.sleep(5)


if __name__ == "__main__":
    run()
