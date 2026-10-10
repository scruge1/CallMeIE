"""Index bounded terminal-call lookups; no row or column changes."""
from alembic import op

revision = '0013_call_event_lookup'
down_revision = '0012_call_status_event_key'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('idx_call_events_call_type', 'call_events', ['call_id', 'event_type'])


def downgrade():
    op.drop_index('idx_call_events_call_type', table_name='call_events')
