"""Quality provenance and logical-unit publication contracts using temporary data."""

from unittest.mock import Mock

import pytest
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.llm.usage import empty_usage
from wenyi_core.pipeline.autofix_publish import AutofixPublisher
from wenyi_core.pipeline.review_results import net_changes
from wenyi_core.review.autofix_models import text_hash
from wenyi_core.review.run_store import ReviewRunStore
from wenyi_core.review.session import ReviewPolicy, ReviewRoundResult, ReviewSessionState
from wenyi_core.storage.file import FileStorage


def test_quality_patch_without_issue_keys_is_not_a_confirmed_issue_fix():
    patch = {
        "patch_id": "quality-1",
        "chapter": 0,
        "index": 0,
        "round": 0,
        "origin": "quality_retranslation",
        "quality_unit_id": "ch0:text0:seg3",
        "quality_decision_ref": "quality/units/ch0:text0:seg3/decision.json",
        "issue_keys": [],
        "status": "accepted_by_comparison",
    }
    state = ReviewSessionState(
        target_overrides={(0, 0): "Revised."},
        patch_records=[patch],
        active_patches={(0, 0): patch},
    )
    state.after_scan(
        ReviewRoundResult([], [], [], [], [], 0), 1, "shadow", ReviewPolicy(False, 1, 1)
    )
    assert patch["status"] == "accepted_by_comparison"
    assert patch["blind_review_checked"] == 1
    changes = net_changes(
        [Chapter(index=0, segments=[Segment(index=3, source="Source.", target="Original.")])],
        state.target_overrides,
        state.patch_records,
        state.active_patches,
    )
    assert changes[0]["provenance"][0]["quality_unit_id"] == "ch0:text0:seg3"
    assert changes[0]["issue_keys"] == []


def test_member_conflict_prevents_partial_logical_unit_publication(tmp_path):
    store = FileStorage(str(tmp_path / "book"))
    store.save_chapter(
        Chapter(
            index=0,
            segments=[
                Segment(index=3, source="First.", target="Original 1", anchor="a"),
                Segment(index=8, source="Second.", target="User edit", cont=True, anchor="b"),
            ],
        )
    )
    debug = ReviewRunStore(store.run_dir, storage=store)
    locations = [
        {
            "chapter": 0,
            "index": i,
            "before": f"Original {i + 1}",
            "before_hash": text_hash(f"Original {i + 1}"),
            "target": f"Candidate {i + 1}",
            "target_hash": text_hash(f"Candidate {i + 1}"),
            "status": "pending",
            "alignment_status": "pending",
            "quality_unit_id": "ch0:text0:seg3",
            "member_refs": ["ch0:text0:seg3", "ch0:text1:seg8"],
        }
        for i in range(2)
    ]
    index = {"status": "applying", "locations": locations, "records": []}
    publisher = AutofixPublisher(Mock(), Mock())
    publisher.apply(store, debug, index, {}, progress=None)
    assert [s.target for s in store.load_chapter(0).segments] == ["Original 1", "User edit"]
    assert all(row["status"] == "failed" for row in locations)
    assert index["status"] == "partial"


@pytest.mark.parametrize("written", [0, 1, 2])
def test_response_and_usage_recover_as_one_journal_without_double_charge(tmp_path, written):
    store = FileStorage(str(tmp_path))
    usage = empty_usage()
    receipt_key = "reviews/review-test/quality/responses/" + "a" * 64 + ".json"
    receipt = {"status": "completed", "response": {"model": "jev-1.13.0"}}
    entries = {"usage.json": usage, receipt_key: receipt}
    store.prepare_usage_commit(entries)
    for key, value in list(entries.items())[:written]:
        store.write_artifact(key, value)
    store.recover_usage()
    store.recover_usage()
    assert store.load_usage() == usage
    assert store.read_artifact(receipt_key) == receipt
    assert store.read_artifact("usage-pending.json") is None


@pytest.mark.parametrize(
    "key",
    [
        "reviews/review-test/quality/responses/../../chapters/0.json",
        "reviews/review-test/quality/responses/not-a-hash.json",
        "reviews/../usage.json",
        "chapters/0.json",
    ],
)
def test_usage_receipts_cannot_write_arbitrary_state(tmp_path, key):
    store = FileStorage(str(tmp_path))
    with pytest.raises(ValueError, match="Invalid usage journal destination"):
        store.prepare_usage_commit({key: {}})


def test_state_lock_is_reentrant_for_atomic_chapter_publication(tmp_path):
    store = FileStorage(str(tmp_path))
    with store.state_lock():
        store.save_chapter(Chapter(index=0, segments=[Segment(index=9, source="One", target="一")]))
        with store.state_lock():
            assert store.load_chapter(0).segments[0].target == "一"
