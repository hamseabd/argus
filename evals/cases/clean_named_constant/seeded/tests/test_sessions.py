from datetime import UTC, datetime, timedelta

from app.sessions import expires_at


def test_a_session_expires_thirty_minutes_after_it_is_created() -> None:
    created = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)

    assert expires_at(created) == created + timedelta(minutes=30)
