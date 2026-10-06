"""The ids, the log and the feedback of the generated "what next" lists (WP6.2)."""

from __future__ import annotations

import pytest
from api_support import T0, make_daemon

from fylgja.core.next_lists import SuggestionNotFound
from fylgja.core.ranking import Candidate

ONE = [Candidate("input", "claude:a", 100.0, "waiting for input")]
TWO = [*ONE, Candidate("todo", "t1", 80.0, "todo marked next in workspace 3: x")]


async def test_a_list_has_an_id_and_its_items_have_ids_made_from_it_and_their_rank() -> None:
    daemon = make_daemon()
    await daemon.start()
    try:
        published = daemon.next_lists.publish(TWO, T0)
        assert published.list_id.startswith("nl-")
        assert len(published.list_id) == len("nl-") + 16
        assert [i.suggestion_id for i in published.items] == [
            f"{published.list_id}.1",
            f"{published.list_id}.2",
        ]
        assert [i.rank for i in published.items] == [1, 2]
        assert published.generated_at == T0
    finally:
        await daemon.stop()


async def test_the_same_ranking_is_the_same_list_and_a_different_one_is_a_new_list() -> None:
    daemon = make_daemon()
    await daemon.start()
    try:
        first = daemon.next_lists.publish(ONE, T0)
        assert daemon.next_lists.publish(ONE, T0) is first
        assert daemon.next_lists.publish(ONE) is first  # even at another time
        other = daemon.next_lists.publish(TWO, T0)
        assert other.list_id != first.list_id
        back = daemon.next_lists.publish(ONE, T0)  # the same instant and content: the same id
        assert back.list_id == first.list_id
        await daemon.writer.flush()
        assert [r.rank for r in await daemon.next_lists.logged(first.list_id)] == [1]
        assert [r.rank for r in await daemon.next_lists.logged(other.list_id)] == [1, 2]
    finally:
        await daemon.stop()


async def test_a_stopped_daemon_publishes_without_logging_or_remembering() -> None:
    daemon = make_daemon()
    await daemon.start()
    await daemon.stop()
    first = daemon.next_lists.publish(ONE, T0)
    assert first.items  # still a list for whoever asks
    again = daemon.next_lists.publish(ONE, T0)
    assert again is not first  # nothing was written, so nothing is reused


async def test_rating_checks_the_rating_and_the_list() -> None:
    daemon = make_daemon()
    await daemon.start()
    try:
        published = daemon.next_lists.publish(TWO, T0)
        with pytest.raises(ValueError, match="good, bad"):
            await daemon.next_lists.rate(published.list_id, "great")  # type: ignore[arg-type]
        with pytest.raises(SuggestionNotFound):
            await daemon.next_lists.rate("nl-unknown", "good")
        rated = await daemon.next_lists.rate(
            published.list_id, "bad", published.items[1].suggestion_id
        )
        assert (rated.rank, rated.rating, rated.feedback_at) == (2, "bad", T0)
    finally:
        await daemon.stop()
