"""Shared local research trial ledger, separate from the financial warehouse."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


@contextmanager
def research_trial(store: Path, inputs_hash: str, parameters: str):
    store.parent.mkdir(parents=True, exist_ok=True)
    trial_id = uuid4().hex
    with sqlite3.connect(store) as ledger:
        ledger.execute(
            "CREATE TABLE IF NOT EXISTS research_trial(trial_id TEXT PRIMARY KEY, inputs_hash TEXT, config TEXT, state TEXT, results TEXT)"
        )
        ledger.execute(
            "INSERT INTO research_trial VALUES (?,?,?,'running',NULL)",
            (trial_id, inputs_hash, parameters),
        )
    summary = {}
    try:
        yield summary
        encoded = json.dumps(summary, allow_nan=False)
    except BaseException as exc:
        with sqlite3.connect(store) as ledger:
            ledger.execute(
                "UPDATE research_trial SET state='failed',results=? WHERE trial_id=?",
                (str(exc), trial_id),
            )
        raise
    with sqlite3.connect(store) as ledger:
        ledger.execute(
            "UPDATE research_trial SET state='complete',results=? WHERE trial_id=?",
            (encoded, trial_id),
        )
