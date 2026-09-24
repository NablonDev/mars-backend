"""Azure Service Bus producer for the CMIR mail-processing queue.

Sends inbound-email payloads for `app.workers.cmir_service_bus_consumer` to
pick up; this module only publishes messages and never reads them back.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from app.core.config import ServiceBusConfig


class ServiceBusMailQueue:
    """Azure Service Bus adapter for the CMIR mail-processing queue."""

    def __init__(self, config: ServiceBusConfig, credential: Any | None = None) -> None:
        self._config = config
        self._credential = credential

    def send(self, payload: dict[str, Any], *, message_id: str) -> None:
        """Send a single message; see `send_many` for the batching behavior."""
        self.send_many([(payload, message_id)])

    def send_many(self, messages: Iterable[tuple[dict[str, Any], str]]) -> None:
        """Send each (payload, message_id) pair as one session-scoped Service Bus message.

        Opens one client and one queue sender for the whole batch, so a large batch
        costs one connection rather than one per message. Every message shares
        `self._config.session_id`, since the queue is session-enabled and
        `app.workers.cmir_service_bus_consumer.run` reads from that same session.
        """
        from azure.servicebus import ServiceBusMessage

        with (
            self._create_client() as client,
            client.get_queue_sender(self._config.queue_name) as sender,
        ):
            for payload, message_id in messages:
                message = ServiceBusMessage(
                    json.dumps(payload),
                    message_id=message_id,
                    content_type="application/json",
                )
                message.session_id = self._config.session_id
                sender.send_messages(message)

    def verify_connection(self) -> None:
        """Open and immediately close a client and sender to confirm the queue is reachable."""
        with self._create_client() as client, client.get_queue_sender(self._config.queue_name):
            return

    def _create_client(self):
        """Build a Service Bus client, preferring a connection string over managed identity.

        A connection string is treated as a local/dev shortcut; production
        deployments omit it so this falls through to Managed Identity /
        Azure AD via `_default_credential`.
        """
        from azure.servicebus import ServiceBusClient

        # Development - Use Connection String
        conn_str = getattr(self._config, "connection_string", None)
        if conn_str:
            return ServiceBusClient.from_connection_string(conn_str=conn_str)

        # Production - Use Managed Identity / Azure AD
        credential = self._credential or self._default_credential()
        return ServiceBusClient(
            fully_qualified_namespace=self._config.fully_qualified_namespace,
            credential=credential,
        )

    @staticmethod
    def _default_credential() -> Any:
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential()
