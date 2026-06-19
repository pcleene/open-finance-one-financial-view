"""Ingestion facade — re-exports the focused modules for backward compatibility.

The ingestion logic was split out of this file (it had grown too large) into
single-responsibility modules; importers can keep using
`acme_ofv.ingestion.service`:

  amounts.py    money/date wire<->storage helpers (d128, amount_to_decimal, ...)
  profile.py    consolidated-profile writes (consent boxes, embed, recent, summary)
  snapshots.py  balance_snapshots (reconstruct primary + synthesize fallback)
  recurring.py  recurring detector
  features.py   uw_features build
  backfill.py   full backfill + transaction pull (LIMITER lives here)
  sync.py       incremental sync
"""

from acme_ofv.ingestion.amounts import (amount_to_decimal, d128, mask_display,
                                        parse_dt)
from acme_ofv.ingestion.backfill import (LIMITER, backfill_consent,
                                         flag_account_error, pull_transactions)
from acme_ofv.ingestion.features import rebuild_uw_features
from acme_ofv.ingestion.profile import (RECENT_CAP, build_consent_boxes,
                                        recent_transactions, refresh_summary,
                                        upsert_embedded_account)
from acme_ofv.ingestion.recurring import detect_recurring
from acme_ofv.ingestion.snapshots import (reconstruct_snapshot_history,
                                          synthesize_snapshot_history)
from acme_ofv.ingestion.sync import incremental_sync_consent

__all__ = [
    "LIMITER", "RECENT_CAP",
    "d128", "amount_to_decimal", "parse_dt", "mask_display",
    "build_consent_boxes", "upsert_embedded_account", "recent_transactions",
    "refresh_summary",
    "synthesize_snapshot_history", "reconstruct_snapshot_history",
    "detect_recurring", "rebuild_uw_features",
    "backfill_consent", "flag_account_error", "pull_transactions",
    "incremental_sync_consent",
]
