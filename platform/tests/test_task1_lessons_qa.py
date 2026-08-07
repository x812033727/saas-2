import pytest

from test_api import create_job


@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "content": "keep real notes"},
        {"title": "manual:blank-content", "content": ""},
        {"title": "   ", "content": "whitespace title should not become a lesson"},
        {"title": "manual:blank-body", "content": "   "},
        {"title": "manual:blank-source", "content": "keep source clean", "source_run_id": "   "},
    ],
)
def test_qa_manual_lesson_rejects_blank_fields_without_writing(client, body):
    job = create_job(client, name="qa-blank-lesson", cron=None)

    resp = client.post(f"/jobs/{job['id']}/lessons", json=body)

    assert resp.status_code == 422, (body, resp.status_code, resp.text)
    assert client.get(f"/jobs/{job['id']}/lessons").json() == []


def test_qa_manual_lesson_rejects_extra_fields_without_writing(client):
    job = create_job(client, name="qa-extra-lesson-field", cron=None)

    resp = client.post(
        f"/jobs/{job['id']}/lessons",
        json={
            "title": "manual:extra-field",
            "content": "should not be partially accepted",
            "job_id": job["id"],
        },
    )

    assert resp.status_code == 422, resp.text
    assert "job_id" in resp.text
    assert client.get(f"/jobs/{job['id']}/lessons").json() == []


def test_qa_manual_lesson_normalizes_outer_whitespace_before_upsert(client):
    job = create_job(client, name="qa-normalized-lesson", cron=None)

    created = client.post(
        f"/jobs/{job['id']}/lessons",
        json={"title": " manual:retry ", "content": " keep logs "},
    )
    assert created.status_code == 201, created.text

    updated = client.post(
        f"/jobs/{job['id']}/lessons",
        json={"title": "manual:retry", "content": "preserve logs"},
    )
    assert updated.status_code == 201, updated.text

    lessons = client.get(f"/jobs/{job['id']}/lessons").json()
    assert len(lessons) == 1, lessons
    assert lessons[0]["id"] == created.json()["id"]
    assert lessons[0]["title"] == "manual:retry"
    assert lessons[0]["content"] == "preserve logs"


def test_qa_manual_lesson_foreign_source_update_is_atomic(client):
    target = create_job(client, name="qa-target", cron=None)
    source = create_job(client, name="qa-source", cron=None)
    source_run = client.post(f"/jobs/{source['id']}/trigger").json()

    created = client.post(
        f"/jobs/{target['id']}/lessons",
        json={"title": "manual:atomic", "content": "original content"},
    )
    assert created.status_code == 201, created.text
    lesson_id = created.json()["id"]

    rejected = client.post(
        f"/jobs/{target['id']}/lessons",
        json={
            "title": "manual:atomic",
            "content": "mutated by rejected request",
            "source_run_id": source_run["id"],
        },
    )

    assert rejected.status_code == 404
    lessons = client.get(f"/jobs/{target['id']}/lessons").json()
    assert lessons == [
        {
            **lessons[0],
            "id": lesson_id,
            "title": "manual:atomic",
            "content": "original content",
            "source_run_id": None,
        }
    ]


def test_qa_manual_lesson_delete_is_job_scoped(client):
    owner = create_job(client, name="qa-owner", cron=None)
    other = create_job(client, name="qa-other", cron=None)
    lesson = client.post(
        f"/jobs/{owner['id']}/lessons",
        json={"title": "manual:owned", "content": "only owner job can delete"},
    ).json()

    wrong_job_delete = client.delete(f"/jobs/{other['id']}/lessons/{lesson['id']}")

    assert wrong_job_delete.status_code == 404
    assert [l["id"] for l in client.get(f"/jobs/{owner['id']}/lessons").json()] == [
        lesson["id"]
    ]
