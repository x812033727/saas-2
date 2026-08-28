from datetime import datetime, timedelta, timezone

import pytest

from ticloud.billing import month_to_date_cost
from ticloud.models import Run, RunStatus

from test_scheduler import make_job


def test_month_to_date_uses_utc_month_for_non_utc_now(session):
    job = make_job(session)
    utc_run_time = datetime(2026, 8, 31, 23, 45, tzinfo=timezone.utc)
    session.add(
        Run(
            job_id=job.id,
            status=RunStatus.SUCCEEDED,
            scheduled_at=utc_run_time,
            started_at=utc_run_time,
            cost_usd=7.0,
        )
    )
    session.commit()

    local_september = datetime(2026, 9, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))

    assert month_to_date_cost(session, [job.id], now=local_september) == pytest.approx(7.0)
