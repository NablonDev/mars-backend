"""IMAP polling client for pulling CMIR dispute emails out of a Gmail inbox."""

from __future__ import annotations

import email as email_lib
import imaplib
from datetime import timedelta

from bs4 import BeautifulSoup

from app.core.config import EmailConfig
from app.schemas.cmir import EmailMessage
from app.utils.clock import utc_now


class GmailImapReader:
    """IMAP client for fetching and acknowledging inbound Gmail messages."""

    def __init__(self, config: EmailConfig) -> None:
        self._config = config

    def _connect(self) -> imaplib.IMAP4_SSL:
        """Open and authenticate a new IMAP session; caller must log out when done."""
        mail = imaplib.IMAP4_SSL(self._config.imap_server, self._config.imap_port)
        mail.login(self._config.address, self._config.password)
        return mail

    def fetch_unread(
        self,
        *,
        limit: int | None = None,
        subject_contains: str | None = None,
        unread_only: bool = True,
    ) -> list[EmailMessage]:
        """Fetch matching messages from INBOX as a fresh IMAP session per call.

        Searches messages since `lookback_days` ago, optionally filtered to
        UNSEEN, and subject-matched against `subject_contains` (or the
        configured default search subject). Only the newest `limit` (or
        `max_per_run`) matches are actually fetched and parsed; earlier
        matches are left untouched on the server. A search failure returns
        an empty list rather than raising.
        """
        mail = self._connect()
        mail.select("INBOX")

        since = (utc_now() - timedelta(days=self._config.lookback_days)).strftime("%d-%b-%Y")
        search_terms = ["SINCE", since, "SUBJECT", subject_contains or self._config.search_subject]
        if unread_only:
            search_terms.insert(0, "UNSEEN")

        # UID-based throughout (search/fetch here, store in mark_as_read) --
        # a plain (non-UID) message *sequence* number is only a snapshot of
        # this message's position in the mailbox at the moment of this
        # SELECT; it silently goes stale (or points at a different message)
        # the instant the mailbox's contents change before the later
        # mark_as_read call -- which, given the real-world delay between
        # ingest and a human's HITL decision, is not a rare edge case. A UID
        # is a stable, permanent identifier for this message that survives
        # across sessions and mailbox changes.
        status, data = mail.uid("search", "", *search_terms)

        if status != "OK":
            mail.logout()
            return []

        email_ids = data[0].split()
        messages: list[EmailMessage] = []

        max_messages = limit or self._config.max_per_run
        for num in email_ids[-max_messages:]:
            status, msg = mail.uid("fetch", num, "(RFC822)")
            if status != "OK":
                continue

            fetch_item = msg[0]
            if not isinstance(fetch_item, tuple):
                continue
            parsed = email_lib.message_from_bytes(fetch_item[1])
            body = self._extract_body(parsed)

            messages.append(
                EmailMessage(
                    imap_id=num.decode(),
                    sender=parsed.get("From", ""),
                    subject=parsed.get("Subject", ""),
                    body=body,
                    source_message_id=parsed.get("Message-ID") or num.decode(),
                )
            )

        mail.logout()
        return messages

    def mark_as_read(self, imap_id: str) -> None:
        """Flag one message as Seen so it's excluded from future unread-only fetches."""
        mail = self._connect()
        mail.select("INBOX")
        mail.uid("store", imap_id, "+FLAGS", "\\Seen")
        mail.logout()

    @staticmethod
    def _extract_body(message: email_lib.message.Message) -> str:
        """Extract readable text from a (possibly multipart) email, preferring plain text.

        For a multipart message, returns the first text/plain or text/html part
        found (HTML is stripped to text via BeautifulSoup); returns "" if
        neither part decodes cleanly or none is present.
        """
        if not message.is_multipart():
            payload = message.get_payload(decode=True)
            return payload.decode(errors="ignore") if isinstance(payload, bytes) else ""

        for part in message.walk():
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue

            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue

            body = payload.decode(errors="ignore")
            if content_type == "text/html":
                body = BeautifulSoup(body, "html.parser").get_text()
            return body

        return ""
