"""Relative mouse queue for PS/2-only guests (rehome/ui/console/relmouse.py)."""

from rehome.ui.console import relmouse
from rehome.ui.console.relmouse import RelMouse

SIZE = (640, 480)
LAYOUT = (10, 20, 640, 480)            # where the guest image is drawn in the view


def moves(rel):
    return [(a, b) for kind, a, b in rel.queue if kind == "rel"]


def test_moves_become_unaccelerated_steps_and_keep_fractions():
    rel = RelMouse()
    rel.motion(25, -13)
    steps = moves(rel)
    assert all(abs(dx) <= relmouse.STEP and abs(dy) <= relmouse.STEP for dx, dy in steps)
    assert sum(d for d, _ in steps) == 25 and sum(d for _, d in steps) == -13
    rel.reset()
    for _ in range(4):
        rel.motion(0.4, 0)                      # 1.6 pixels: one step now, 0.6 kept
    assert moves(rel) == [(1, 0)]
    rel.motion(0.4, 0)
    assert moves(rel) == [(1, 0), (1, 0)]
    rel.reset()
    rel.motion(0, 0)
    assert not rel.pending()


def test_edges_push_once_per_arrival_far_enough_for_the_screen():
    rel = RelMouse()
    assert not rel.edge(300, 200, LAYOUT, SIZE)
    assert rel.edge(10, 200, LAYOUT, SIZE)                       # left edge of the image
    pushes = [i for i in rel.queue if i[0] == "push"]
    assert {(i[1], i[2]) for i in pushes} == {(-relmouse.PACKET, 0)}
    assert len(pushes) * relmouse.PACKET >= SIZE[0]
    rel.queue.clear()
    assert not rel.edge(10, 300, LAYOUT, SIZE)                   # still there
    assert rel.edge(649, 300, LAYOUT, SIZE)                      # right edge
    rel.queue.clear()
    assert rel.edge(649, 499, LAYOUT, SIZE)                      # adds the bottom
    assert {(i[1], i[2]) for i in rel.queue} == {(0, relmouse.PACKET)}
    rel.queue.clear()
    assert not rel.edge(300, 200, LAYOUT, SIZE)
    assert rel.edge(0, 0, LAYOUT, SIZE)                          # letterbox: past the corner
    assert {(i[1], i[2]) for i in rel.queue} == {(-relmouse.PACKET, -relmouse.PACKET)}


def test_leaving_the_view_pushes_to_the_nearest_side():
    for (x, y), push in [((330, 495), (0, 1)), ((15, 260), (-1, 0)), ((640, 30), (1, 0)),
                         ((330, 22), (0, -1))]:
        rel = RelMouse()
        rel.left(x, y, LAYOUT, SIZE)
        assert {(i[1], i[2]) for i in rel.queue} == {(push[0] * relmouse.PACKET,
                                                      push[1] * relmouse.PACKET)}


def test_align_goes_to_the_corner_then_across():
    rel = RelMouse()
    rel.align(100, 50, SIZE)
    kinds = [i[0] for i in rel.queue]
    assert kinds[0] == "push" and set(kinds[kinds.index("rel"):]) == {"rel"}
    assert sum(d for d, _ in moves(rel)) == 100 and sum(d for _, d in moves(rel)) == 50


def test_bursts_and_catching_up_after_a_flick():
    rel = RelMouse()
    rel.motion(12, 0)
    rel.button(1, True)
    rel.button(1, False)
    out = rel.take()
    assert len(out) == relmouse.BURST and out[-1] == ("btn", 1, True)
    rel.reset()
    rel.edge(10, 200, LAYOUT, SIZE)              # a push, then far too many steps
    for _ in range(100):
        rel.motion(6, 1)
    rel.button(1, True)
    rel.motion(6, 0)
    assert len(rel.queue) > relmouse.BACKLOG
    rel.take(0)                                  # merges
    items = list(rel.queue)
    assert len(items) < relmouse.BACKLOG
    first_rel = next(i for i, it in enumerate(items) if it[0] == "rel")
    assert all(it[0] == "push" for it in items[:first_rel])       # the push stays first
    btn = items.index(("btn", 1, True))
    before = [it for it in items[:btn] if it[0] == "rel"]
    assert sum(it[1] for it in before) == 600 and sum(it[2] for it in before) == 100
    assert all(abs(it[1]) <= relmouse.PACKET for it in items if it[0] == "rel")
    assert items[btn + 1:] == [("rel", 6, 0)]
