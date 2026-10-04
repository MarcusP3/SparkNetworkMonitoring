"""The map's Diagram view: where diagram.py puts things, and what it groups.

Layout is plain arithmetic on the map tree, so it is tested without a
database: devices built in memory, laid out, and the boxes checked against
each other -- nothing overlaps, every child sits under its parent, and the
lines that lead to trouble take its colour.
"""

from __future__ import annotations

import itertools

from spark import diagram
from spark.models import Device, DeviceRole
from spark.servicemap import Node, State

UP = State("up", "ok", "test")
DOWN = State("down", "bad", "test")
SLOW = State("degraded", "warn", "test")
QUIET = State("not watched", "neutral", "test")
_ids = itertools.count(1)


def node(name: str, role: DeviceRole, state: State = UP, children=()) -> Node:  # type: ignore[no-untyped-def]
    device = Device(id=next(_ids), friendly_name=name, primary_ip=f"172.16.10.{next(_ids)}", role=role)
    return Node(device=device, state=state, role=str(role), services=[], children=list(children))


def home(server_state: State = UP) -> Node:
    servers = [node(f"srv-{i}", DeviceRole.HOST, server_state if i == 0 else UP) for i in range(5)]
    phones = [node(f"phone-{i}", DeviceRole.CLIENT, QUIET) for i in range(4)]
    ap = node("ap", DeviceRole.ACCESS_POINT, children=phones)
    switch = node("core-switch", DeviceRole.SWITCH, children=[*servers, ap])
    return node("gateway", DeviceRole.GATEWAY, children=[switch])


def boxes(d: diagram.Diagram):  # type: ignore[no-untyped-def]
    return {i.node.device.display_name if i.node else i.title: i for i in d.items}


def overlap(a, b) -> bool:  # type: ignore[no-untyped-def]
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h


class TestShape:
    def test_servers_are_grouped_and_end_devices_too(self):
        d = diagram.layout([home()])
        named = boxes(d)
        assert "Servers · 5" in named and "4 devices" in named
        assert [c.label for c in named["Servers · 5"].chips][:2] == ["srv-0", "srv-1"]
        assert {"gateway", "core-switch", "ap"} <= set(named), "carriers are boxes"
        assert not any(k.startswith("srv-") for k in named), "no server gets a box of its own"

    def test_one_server_is_a_box_not_a_group(self):
        switch = node("sw", DeviceRole.SWITCH, children=[node("nas", DeviceRole.NAS)])
        named = boxes(diagram.layout([switch]))
        assert "nas" in named and not any(k.startswith("Servers") for k in named)

    def test_a_server_with_something_below_it_is_a_box(self):
        vm = node("vm", DeviceRole.CLIENT)
        host = node("hv", DeviceRole.HOST, children=[vm])
        other = node("other", DeviceRole.HOST)
        lone = node("lone", DeviceRole.HOST)
        named = boxes(diagram.layout([node("sw", DeviceRole.SWITCH, children=[host, other, lone])]))
        assert "hv" in named and "Servers · 2" in named and "1 device" in named

    def test_a_big_group_says_how_many_more(self):
        many = [node(f"p{i}", DeviceRole.CLIENT) for i in range(diagram.MAX_CHIPS + 7)]
        group = boxes(diagram.layout([node("ap", DeviceRole.ACCESS_POINT, children=many)]))[
            f"{diagram.MAX_CHIPS + 7} devices"]
        assert len(group.chips) == diagram.MAX_CHIPS and group.more == 7

    def test_long_names_are_cut(self):
        long = node("a-very-long-device-name-indeed", DeviceRole.CLIENT)
        group = boxes(diagram.layout([node("ap", DeviceRole.ACCESS_POINT, children=[long])]))["1 device"]
        assert group.chips[0].label == "a-very-long-devi…" and len(group.chips[0].label) == diagram.NAME_CHARS


class TestPlacement:
    def test_nothing_overlaps_and_children_sit_below(self):
        d = diagram.layout([home(), node("second-gw", DeviceRole.GATEWAY)])
        for a, b in itertools.combinations(d.items, 2):
            assert not overlap(a, b), (a.title or a.node.device.display_name, b.title or b.node.device.display_name)
        for item in d.items:
            for child in item.children:
                assert child.y >= item.y + item.h + diagram.GAP_Y - 0.01
            assert item.x >= 0 and item.x + item.w <= d.width and item.y + item.h <= d.height

    def test_a_parent_is_centred_over_the_room_its_children_take(self):
        named = boxes(diagram.layout([home()]))
        switch = named["core-switch"]
        # Each child is centred in its own subtree's width; the parent over
        # all of them together.
        left = min(c.cx - diagram._width(c) / 2 for c in switch.children)
        right = max(c.cx + diagram._width(c) / 2 for c in switch.children)
        assert abs(switch.cx - (left + right) / 2) < 1

    def test_the_same_map_draws_the_same_way(self):
        a, b = diagram.layout([home()]), diagram.layout([home()])
        assert [(i.x, i.y) for i in a.items] == [(i.x, i.y) for i in b.items]
        assert [e.d for e in a.edges] == [e.d for e in b.edges]

    def test_nothing_placed_is_an_empty_drawing(self):
        d = diagram.layout([])
        assert d.items == [] and d.edges == [] and d.width == diagram.MARGIN


class TestTrouble:
    def test_the_path_to_a_down_server_is_red(self):
        d = diagram.layout([home(DOWN)])
        kinds = [e.kind for e in d.edges]
        # gateway -> switch, switch -> servers: red. switch -> ap, ap -> phones: quiet.
        assert kinds.count("bad") == 2 and kinds.count(None) == 2
        assert kinds[-2:] == ["bad", "bad"], "trouble is drawn over the quiet lines"
        assert boxes(d)["Servers · 5"].problem == "bad"

    def test_degraded_is_amber_and_down_wins(self):
        assert {e.kind for e in diagram.layout([home(SLOW)]).edges} == {"warn", None}
        switch = node("sw", DeviceRole.SWITCH, children=[
            node("a", DeviceRole.HOST, DOWN), node("b", DeviceRole.HOST, SLOW)])
        assert diagram.trouble_below(switch) == "bad"

    def test_a_quiet_network_has_no_coloured_lines(self):
        assert {e.kind for e in diagram.layout([home()]).edges} == {None}
