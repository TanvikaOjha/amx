"""SQLite ledger: durable channel state, replay protection and idempotency (protocol section 3).

Lifecycle of one paid call
    reserve()   atomically checks nonce / cumulative monotonicity and advances the channel
                state BEFORE the job runs, so two concurrent requests can never spend the same
                voucher.  Returns Reserved, or Replay for an exact retry of a finished call.
    complete()  stores the response against the voucher so retries are served without a charge.
    release()   if the job itself failed, undo the reservation (compare-and-set) so the client
                is not charged for a result it never got and can retry the same voucher.

Every accepted voucher is kept, newest per channel in `channels`, because the payee must later
present the latest one to PaymentChannel.close()/submitVoucher() to get paid.

Amounts are stored as decimal TEXT (uint256 does not fit SQLite integers).
Single-writer assumption for v1: run ONE gateway process per database file.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .schemas import REASON_CODES

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id      TEXT PRIMARY KEY,
    last_nonce      INTEGER NOT NULL,
    last_cumulative TEXT    NOT NULL,
    voucher_json    TEXT    NOT NULL,
    signature       TEXT    NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vouchers (
    channel_id        TEXT    NOT NULL,
    nonce             INTEGER NOT NULL,
    request_hash      TEXT    NOT NULL,
    cumulative        TEXT    NOT NULL,
    signer            TEXT    NOT NULL,
    voucher_json      TEXT    NOT NULL,
    signature         TEXT    NOT NULL,
    status            TEXT    NOT NULL CHECK (status IN ('reserved', 'completed')),
    response_json     TEXT,
    prev_nonce        INTEGER NOT NULL,
    prev_cumulative   TEXT    NOT NULL,
    prev_voucher_json TEXT,
    prev_signature    TEXT,
    created_at        INTEGER NOT NULL,
    PRIMARY KEY (channel_id, nonce)
);
"""


class LedgerRejection(Exception):
    def __init__(self, reason: REASON_CODES) -> None:
        super().__init__(reason)
        self.reason = reason


class InProgress(Exception):
    """An identical request is currently being processed."""


@dataclass(frozen=True)
class ChannelState:
    last_nonce: int
    last_cumulative: int


@dataclass(frozen=True)
class Reserved:
    channel_id: str
    nonce: int
    cumulative: int


@dataclass(frozen=True)
class Replay:
    response_json: str


class Ledger:
    def __init__(self, path: str) -> None:
        self._path = path
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    # -- plumbing -----------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- reads --------------------------------------------------------------------
    def get_state(self, channel_id: str) -> ChannelState:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT last_nonce, last_cumulative FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return ChannelState(0, 0)
        return ChannelState(int(row["last_nonce"]), int(row["last_cumulative"]))

    def latest_voucher(self, channel_id: str) -> tuple[dict, str] | None:
        """Newest accepted voucher + signature: what the payee presents on-chain to settle."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT voucher_json, signature FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else (json.loads(row["voucher_json"]), row["signature"])

    def lookup_replay(
        self, channel_id: str, nonce: int, request_hash: str, cumulative: int, signer: str
    ) -> Replay | None:
        """Exact retry of an already-finished call -> Replay.  Still running -> InProgress."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM vouchers WHERE channel_id = ? AND nonce = ?", (channel_id, nonce)
            ).fetchone()
        finally:
            conn.close()
        return self._interpret_existing(row, request_hash, cumulative, signer, strict=False)

    @staticmethod
    def _interpret_existing(
        row: sqlite3.Row | None, request_hash: str, cumulative: int, signer: str, *, strict: bool
    ) -> Replay | None:
        if row is None:
            return None
        exact = (
            row["request_hash"] == request_hash
            and int(row["cumulative"]) == cumulative
            and row["signer"] == signer
        )
        if not exact:
            if strict:
                raise LedgerRejection("nonce_not_increasing")  # nonce reused for different content
            return None
        if row["status"] == "completed" and row["response_json"] is not None:
            return Replay(row["response_json"])
        raise InProgress()

    # -- writes -------------------------------------------------------------------
    def reserve(
        self,
        *,
        channel_id: str,
        nonce: int,
        cumulative: int,
        request_hash: str,
        signer: str,
        voucher_json: str,
        signature: str,
        price: int,
    ) -> Reserved | Replay:
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM vouchers WHERE channel_id = ? AND nonce = ?", (channel_id, nonce)
            ).fetchone()
            replay = self._interpret_existing(existing, request_hash, cumulative, signer, strict=True)
            if replay is not None:
                return replay

            state_row = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
            last_nonce = int(state_row["last_nonce"]) if state_row else 0
            last_cumulative = int(state_row["last_cumulative"]) if state_row else 0

            if nonce <= last_nonce:
                raise LedgerRejection("nonce_not_increasing")
            if cumulative < last_cumulative + price:
                raise LedgerRejection("insufficient_cumulative_amount")

            now = int(time.time())
            conn.execute(
                """INSERT INTO vouchers
                   (channel_id, nonce, request_hash, cumulative, signer, voucher_json, signature, status,
                    prev_nonce, prev_cumulative, prev_voucher_json, prev_signature, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?)""",
                (
                    channel_id,
                    nonce,
                    request_hash,
                    str(cumulative),
                    signer,
                    voucher_json,
                    signature,
                    last_nonce,
                    str(last_cumulative),
                    state_row["voucher_json"] if state_row else None,
                    state_row["signature"] if state_row else None,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO channels (channel_id, last_nonce, last_cumulative, voucher_json, signature, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(channel_id) DO UPDATE SET
                     last_nonce = excluded.last_nonce,
                     last_cumulative = excluded.last_cumulative,
                     voucher_json = excluded.voucher_json,
                     signature = excluded.signature,
                     updated_at = excluded.updated_at""",
                (channel_id, nonce, str(cumulative), voucher_json, signature, now),
            )
        return Reserved(channel_id, nonce, cumulative)

    def complete(self, channel_id: str, nonce: int, response_json: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE vouchers SET status = 'completed', response_json = ? WHERE channel_id = ? AND nonce = ?",
                (response_json, channel_id, nonce),
            )

    def update_response(self, channel_id: str, nonce: int, response_json: str) -> None:
        """Replace the stored response of a completed call (used when HCS confirmation lands later)."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE vouchers SET response_json = ? WHERE channel_id = ? AND nonce = ? AND status = 'completed'",
                (response_json, channel_id, nonce),
            )

    def release(self, channel_id: str, nonce: int) -> bool:
        """Undo a reservation after a failed job.  Only rolls the channel back if no later voucher
        has been accepted in the meantime (compare-and-set); returns whether state was restored."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM vouchers WHERE channel_id = ? AND nonce = ? AND status = 'reserved'",
                (channel_id, nonce),
            ).fetchone()
            if row is None:
                return False

            state = conn.execute("SELECT last_nonce FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
            restored = False
            if state is not None and int(state["last_nonce"]) == nonce:
                if int(row["prev_nonce"]) == 0 or row["prev_voucher_json"] is None:
                    conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
                else:
                    conn.execute(
                        """UPDATE channels SET last_nonce = ?, last_cumulative = ?, voucher_json = ?,
                           signature = ?, updated_at = ? WHERE channel_id = ?""",
                        (
                            int(row["prev_nonce"]),
                            row["prev_cumulative"],
                            row["prev_voucher_json"],
                            row["prev_signature"],
                            int(time.time()),
                            channel_id,
                        ),
                    )
                restored = True
            conn.execute("DELETE FROM vouchers WHERE channel_id = ? AND nonce = ?", (channel_id, nonce))
            return restored