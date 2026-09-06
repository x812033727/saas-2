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


def test_qa_manual_lesson_patch_updates_fields_and_can_clear_source_run(client):
    job = create_job(client, name="qa-patch-lesson", cron=None)
    source_run = client.post(f"/jobs/{job['id']}/trigger").json()
    lesson = client.post(
        f"/jobs/{job['id']}/lessons",
        json={
            "title": "manual:old",
            "content": "old content",
            "source_run_id": source_run["id"],
        },
    ).json()

    patched = client.patch(
        f"/jobs/{job['id']}/lessons/{lesson['id']}",
        json={"title": " manual:new ", "content": " clearer content ", "source_run_id": None},
    )

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["id"] == lesson["id"]
    assert body["title"] == "manual:new"
    assert body["content"] == "clearer content"
    assert body["source_run_id"] is None


def test_qa_manual_lesson_patch_rejects_duplicate_title_without_mutating(client):
    job = create_job(client, name="qa-patch-duplicate", cron=None)
    first = client.post(
        f"/jobs/{job['id']}/lessons",
        json={"title": "manual:first", "content": "keep first"},
    ).json()
    second = client.post(
        f"/jobs/{job['id']}/lessons",
        json={"title": "manual:second", "content": "keep second"},
    ).json()

    rejected = client.patch(
        f"/jobs/{job['id']}/lessons/{second['id']}",
        json={"title": first["title"], "content": "mutated"},
    )

    assert rejected.status_code == 409
    lessons = client.get(f"/jobs/{job['id']}/lessons").json()
    by_id = {lesson["id"]: lesson for lesson in lessons}
    assert by_id[first["id"]]["content"] == "keep first"
    assert by_id[second["id"]]["title"] == "manual:second"
    assert by_id[second["id"]]["content"] == "keep second"


@pytest.mark.parametrize(
    "body",
    [
        {"title": None},
        {"content": None},
        {"title": "   "},
        {"content": "   "},
        {"source_run_id": "   "},
        {"job_id": "wrong-place"},
    ],
)
def test_qa_manual_lesson_patch_rejects_invalid_fields_without_mutating(client, body):
    job = create_job(client, name="qa-invalid-patch-lesson", cron=None)
    lesson = client.post(
        f"/jobs/{job['id']}/lessons",
        json={"title": "manual:stable", "content": "stable content"},
    ).json()

    rejected = client.patch(f"/jobs/{job['id']}/lessons/{lesson['id']}", json=body)

    assert rejected.status_code == 422, (body, rejected.status_code, rejected.text)
    lessons = client.get(f"/jobs/{job['id']}/lessons").json()
    assert lessons == [
        {
            **lessons[0],
            "id": lesson["id"],
            "title": "manual:stable",
            "content": "stable content",
            "source_run_id": None,
        }
    ]


def test_qa_manual_lesson_patch_foreign_source_update_is_atomic(client):
    target = create_job(client, name="qa-patch-target", cron=None)
    source = create_job(client, name="qa-patch-source", cron=None)
    source_run = client.post(f"/jobs/{source['id']}/trigger").json()
    lesson = client.post(
        f"/jobs/{target['id']}/lessons",
        json={"title": "manual:atomic-patch", "content": "original content"},
    ).json()

    rejected = client.patch(
        f"/jobs/{target['id']}/lessons/{lesson['id']}",
        json={"content": "mutated by rejected patch", "source_run_id": source_run["id"]},
    )

    assert rejected.status_code == 404
    lessons = client.get(f"/jobs/{target['id']}/lessons").json()
    assert lessons == [
        {
            **lessons[0],
            "id": lesson["id"],
            "title": "manual:atomic-patch",
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
