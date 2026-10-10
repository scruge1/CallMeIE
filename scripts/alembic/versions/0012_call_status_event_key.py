"""Add atomic status-event dedupe to the existing call_events table.

Existing events remain unchanged with NULL keys. No new event store.
Apply through the existing migration owner before deploying the receiver.
"""
import sqlalchemy as sa
from alembic import op

revision = '0012_call_status_event_key'
down_revision = '0011_d5_build_queue'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('call_events', sa.Column('event_key', sa.Text(), nullable=True))
    op.create_index('idx_call_events_event_key', 'call_events', ['event_key'], unique=True)


def downgrade():
    # Preserve status history; only remove the dedupe field/index.
    op.drop_index('idx_call_events_event_key', table_name='call_events')
    op.drop_column('call_events', 'event_key')
