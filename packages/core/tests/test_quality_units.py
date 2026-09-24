from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.quality.units import build_quality_units


def test_units_keep_view_index_segment_identity_and_continuations():
    chapter = Chapter(
        index=4,
        segments=[
            Segment(index=8, source="  "),
            Segment(index=20, source="Title", target="标题", kind="heading"),
            Segment(index=42, source="First ", target="第一"),
            Segment(index=51, source="second.", target="第二。", cont=True),
        ],
    )
    units = build_quality_units([chapter])
    assert len(units) == 2
    assert units[0].kind == "heading"
    assert units[1].text_indices == (1, 2)
    assert units[1].segment_indices == (42, 51)
    assert units[1].source_parts == ("First ", "second.")
    assert chapter.segments[2].target == "第一"


def test_pending_and_empty_targets_are_distinct():
    units = build_quality_units(
        [
            Chapter(
                index=0,
                segments=[
                    Segment(index=0, source="pending", target=None),
                    Segment(index=1, source="empty", target=""),
                ],
            )
        ]
    )
    assert units[0].status == "pending"
    assert units[1].status == "needs_review"
    assert units[1].reason_codes == ("empty_target",)
