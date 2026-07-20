"""add used_creative uniqueness registry

The uniqueness engine's ledger: one row per creative value the system has
committed (mix titles, thumbnail hook texts, scene descriptors) so no title,
hook, or scene is ever repeated across the catalog.

* New ``used_creative`` table: ``kind`` ("title" | "hook" | "scene"),
  ``value_normalized`` (indexed comparison key: lowercased, punctuation
  stripped, series prefix removed for titles), ``value_raw``, nullable
  ``mix_id``, ``created_at``.
* Seed backfill: every existing mix title plus every approved/applying/applied
  title proposal is registered as ``kind="title"`` (deduped on the normalized
  value, first claimant wins). Hooks/scenes have no reliable historical record
  — pre-engine thumbnails all carried the fixed per-genre motif hook — so the
  hook/scene ledger starts empty and fills as art is (re)generated.

Revision ID: 0006_add_used_creative
Revises: 0005_add_catalog_and_proposals
Create Date: 2026-07-19
"""
import re
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_add_used_creative"
down_revision: Union[str, None] = "0005_add_catalog_and_proposals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Normalization inlined (migrations must not drift with app code): must match
# app.services.uniqueness.normalize for kind="title".
_SERIES_PREFIX_RE = re.compile(
    r"^\s*(?:will\s*see\s*wednesdays?|(?:2nd|second)\s*saturdays?)\s*[:|\-–—]+\s*",
    re.IGNORECASE,
)


def _normalize_title(value: str) -> str:
    v = (value or "").strip()
    v = _SERIES_PREFIX_RE.sub("", v)
    v = v.lower()
    v = re.sub(r"[^\w\s]", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def upgrade() -> None:
    op.create_table(
        "used_creative",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("value_normalized", sa.String(), nullable=False),
        sa.Column("value_raw", sa.Text(), nullable=False),
        sa.Column(
            "mix_id",
            sa.String(),
            sa.ForeignKey("mixes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_used_creative_kind", "used_creative", ["kind"])
    op.create_index(
        "ix_used_creative_value_normalized", "used_creative", ["value_normalized"]
    )
    op.create_index("ix_used_creative_mix_id", "used_creative", ["mix_id"])

    # --- Seed: existing mix titles + approved/applied title proposals --------
    conn = op.get_bind()
    now = datetime.now(timezone.utc)
    seen: set = set()

    def _seed(raw: str, mix_id) -> None:
        norm = _normalize_title(raw)
        if not norm or norm in seen:
            return
        seen.add(norm)
        conn.execute(
            sa.text(
                "INSERT INTO used_creative "
                "(kind, value_normalized, value_raw, mix_id, created_at) "
                "VALUES ('title', :norm, :raw, :mix_id, :ts)"
            ),
            {"norm": norm, "raw": raw, "mix_id": mix_id, "ts": now},
        )

    for mix_id, title in conn.execute(
        sa.text("SELECT id, title FROM mixes ORDER BY created_at, id")
    ).fetchall():
        _seed(title or "", mix_id)

    for mix_id, proposed in conn.execute(
        sa.text(
            "SELECT mix_id, proposed_value FROM mix_proposals "
            "WHERE field = 'title' "
            "AND status IN ('approved', 'applying', 'applied') "
            "ORDER BY created_at, id"
        )
    ).fetchall():
        _seed(proposed or "", mix_id)


def downgrade() -> None:
    op.drop_index("ix_used_creative_mix_id", table_name="used_creative")
    op.drop_index("ix_used_creative_value_normalized", table_name="used_creative")
    op.drop_index("ix_used_creative_kind", table_name="used_creative")
    op.drop_table("used_creative")
