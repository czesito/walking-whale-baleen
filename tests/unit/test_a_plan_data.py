"""Planner keeps an attachment's own data (sha256) when its probe adds route data."""

from __future__ import annotations

from baleen.model import Action, Category, PlanItem, Probe
from baleen.plan import Planner, _Node

from .a_support import probe_ctx


def test_probe_data_is_merged_not_replaced(home) -> None:  # noqa: ANN001
    item = PlanItem(n=2, source_path="mail.eml#anim.gif", abs_path=None, size=10, mtime_ns=None, ext=".gif",
                    category=Category.IMAGE, action=Action.NONE, route="image", target_ext=".jpg", out_dir="",
                    output_path=None, parent=1, depth=1, data={"sha256": "ab" * 32, "child": {"cid": "x"}})
    nd = _Node(item, probe=Probe(Category.IMAGE, ".jpg", Action.CONVERT, reasons=["MULTI_FRAME_IMAGE"],
                                 data={"w": 8, "h": 8}))
    Planner(probe_ctx(home), "C:/src", "C:/out")._apply_probe(nd)
    assert item.final and item.data == {"sha256": "ab" * 32, "child": {"cid": "x"}, "w": 8, "h": 8}
