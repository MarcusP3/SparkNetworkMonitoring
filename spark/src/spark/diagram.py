"""The network map drawn as a diagram: where every box goes, worked out here.

The List view shows the map as a tree of rows; this lays the same tree out
top-down -- gateway at the top, then what plugs into it -- for the Diagram
view. Layout happens on the server so the page stays a template and the live
refresh swaps it like any other part of #live; the browser only pans and
zooms the finished drawing.

Three kinds of thing get drawn:

  * **Boxes** for the devices that carry others: gateways, switches, access
    points, and anything with something placed below it.
  * **A server group** under a parent for its childless servers, NAS and
    UPS, when there are two or more: fourteen servers in a row of fourteen
    boxes is a diagram you have to scroll sideways to read.
  * **A device group** for the end devices (phones, cameras, laptops), as
    small named tiles.

Lines run from each parent down to its children and take the worst trouble
anywhere below them, so a down host three switches away lights the path to
it in red: you see where the problem is before you find it.

A plain tidy-tree layout: every subtree is as wide as the wider of its own
box and its children side by side, and each box is centred over the room its
subtree takes. Good enough for a home network's tree, and predictable: the
same map draws the same way every time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import DeviceRole
from .servicemap import PROBLEM, Node

NODE_W, NODE_H = 184, 54
CHIP_W, CHIP_H, CHIP_GAP = 128, 24, 6
GROUP_PAD, GROUP_HEAD = 10, 28
GROUP_COLS = 3
# A group shows this many tiles at most, then "+N more": past that a group is
# a list, and the List view is the better place to read it.
MAX_CHIPS = 18
GAP_X, GAP_Y = 26, 58
MARGIN = 24
NAME_CHARS = 17      # a tile's name, cut with an ellipsis past this
BOX_NAME_CHARS = 18

# Drawn as a box even with nothing below it: the shape of the network. A
# hypervisor carries its VMs and containers, so it is one too.
CARRIERS = {DeviceRole.GATEWAY, DeviceRole.SWITCH, DeviceRole.ACCESS_POINT, DeviceRole.HYPERVISOR}


def short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def worst(kinds) -> str | None:  # type: ignore[no-untyped-def]
    """'bad' over 'warn' over nothing."""
    kinds = set(kinds)
    return "bad" if "bad" in kinds else "warn" if "warn" in kinds else None


def trouble(node: Node) -> str | None:
    """The worst problem on this device itself: its own state, or a watched
    port's."""
    own = [node.state.kind] + [s.state.kind for s in node.watched if s.state is not None]
    return worst(k for k in own if k in PROBLEM)


def trouble_below(node: Node) -> str | None:
    """The worst problem on this device or anything placed under it."""
    return worst([trouble(node)] + [trouble_below(c) for c in node.children])


@dataclass
class Chip:
    node: Node
    label: str
    x: float = 0
    y: float = 0
    w: float = CHIP_W
    h: float = CHIP_H


@dataclass
class Item:
    kind: str                      # "node" or "group"
    w: float
    h: float
    node: Node | None = None       # a box's device
    title: str = ""                # a group's heading
    chips: list[Chip] = field(default_factory=list)
    more: int = 0                  # a group's tiles not shown
    problem: str | None = None     # worst trouble in this box or below it
    children: list[Item] = field(default_factory=list)
    x: float = 0
    y: float = 0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2


@dataclass
class Edge:
    d: str
    kind: str | None               # "bad", "warn" or None


@dataclass
class Diagram:
    width: float
    height: float
    items: list[Item]              # every box and group, parents first
    edges: list[Edge]


def _group(members: list[Node], title: str) -> Item:
    shown = members[:MAX_CHIPS]
    cols = max(1, min(GROUP_COLS, len(shown)))
    rows = math.ceil(len(shown) / cols)
    more = len(members) - len(shown)
    w = cols * CHIP_W + (cols - 1) * CHIP_GAP + 2 * GROUP_PAD
    h = GROUP_HEAD + rows * CHIP_H + (rows - 1) * CHIP_GAP + GROUP_PAD + (18 if more else 0)
    item = Item("group", w, h, title=title, more=more,
                chips=[Chip(n, short(n.device.display_name, NAME_CHARS)) for n in shown],
                problem=worst(trouble_below(n) for n in members))
    for i, chip in enumerate(item.chips):
        chip.x = GROUP_PAD + (i % cols) * (CHIP_W + CHIP_GAP)
        chip.y = GROUP_HEAD + (i // cols) * (CHIP_H + CHIP_GAP)
    return item


def _build(node: Node) -> Item:
    item = Item("node", NODE_W, NODE_H, node=node, problem=trouble_below(node))
    carriers = [c for c in node.branches if c.children or c.device.role in CARRIERS]
    servers = [c for c in node.branches if c not in carriers]
    if len(servers) >= 2:
        item.children.append(_group(servers, f"Servers · {len(servers)}"))
    else:
        item.children.extend(_build(s) for s in servers)
    item.children.extend(_build(c) for c in carriers)
    if node.leaves:
        n = len(node.leaves)
        item.children.append(_group(node.leaves, f"{n} device{'s' if n != 1 else ''}"))
    return item


def _width(item: Item) -> float:
    if not item.children:
        return item.w
    return max(item.w, sum(_width(c) for c in item.children) + GAP_X * (len(item.children) - 1))


def _place(item: Item, left: float, top: float, out: list[Item], edges: list[Edge]) -> None:
    span = _width(item)
    item.x, item.y = left + (span - item.w) / 2, top
    out.append(item)
    if not item.children:
        return
    total = sum(_width(c) for c in item.children) + GAP_X * (len(item.children) - 1)
    x = left + (span - total) / 2
    child_top = top + item.h + GAP_Y
    for child in item.children:
        _place(child, x, child_top, out, edges)
        x += _width(child) + GAP_X
        mid = item.y + item.h + GAP_Y / 2
        edges.append(Edge(
            f"M{item.cx:.0f} {item.y + item.h:.0f}V{mid:.0f}H{child.cx:.0f}V{child.y:.0f}",
            child.problem))


def layout(roots: list[Node]) -> Diagram:
    """Every root side by side, each tree laid out under it."""
    items: list[Item] = []
    edges: list[Edge] = []
    x = MARGIN
    for root in roots:
        tree = _build(root)
        _place(tree, x, MARGIN, items, edges)
        x += _width(tree) + GAP_X * 2
    width = max([i.x + i.w for i in items], default=0) + MARGIN
    height = max([i.y + i.h for i in items], default=0) + MARGIN
    # A problem's path is drawn over the quiet lines, not under them.
    edges.sort(key=lambda e: {"bad": 2, "warn": 1}.get(e.kind or "", 0))
    return Diagram(width, height, items, edges)
