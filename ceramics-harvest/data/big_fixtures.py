"""大册压测数据集：2 万 / 8 万登记记录的发布版，固定每页 500 条。

仅在环境变量 CERAMICS_BIG_LEDGER=1 时由 fixtures 挂载，普通验证与日常运行不加载，
避免模拟站进程平白占用大册内存。数据完全确定性，不依赖外部资源。

设计要点：
- 两个大册版本都把 page_size 固定为 parsing.PAGE_SIZE（500），
  8 万版因此有 160 页，而每页正文/记录数与 2 万版完全同量级；
- 拼接清单为一条覆盖前一半陶片的长链，引用编号必然存在于同版登记中，
  保证大册可以通过引用完整性校验并整体激活；
- 后一半陶片故意不参与拼接，用于校验"孤立片"连通组在大册下仍正确。
"""
from __future__ import annotations

from ceramics.parsing import PAGE_SIZE

MUSEUMS = ("合作馆甲", "合作馆乙", "合作馆丙")
MATERIALS = ("夹砂陶", "泥质陶", "彩陶", "黑陶")


def _catalog(i: int) -> str:
    return f"B-{i:06d}"


def _build_registry(total: int) -> list[dict]:
    rows = []
    for i in range(total):
        rows.append({
            "catalog_no": _catalog(i),
            "museum": MUSEUMS[i % len(MUSEUMS)],
            "material": MATERIALS[i % len(MATERIALS)],
            "weight_g": round(50.0 + (i * 7) % 900 + i % 10 / 10.0, 1),
        })
    return rows


def _build_joins(total: int) -> list[dict]:
    # 长链覆盖前一半编号：B-000000~B-000001~...，边数 = total // 2 - 1
    edge_count = total // 2
    rows = []
    for i in range(edge_count - 1):
        rows.append({
            "a": _catalog(i),
            "b": _catalog(i + 1),
            "bond": f"大册链拼接-{i // 100:03d}",
            "confidence": ("high", "medium", "low")[i % 3],
        })
    return rows


def _big_version(tag: str, total: int, published_at: str, layout: str, note: str) -> dict:
    return {
        "published_at": published_at,
        "note": note,
        "layouts": {"registry": layout, "joins": layout},
        # 固定每页 500 条：2 万版 40 页、8 万版 160 页，单页体量一致
        "page_size": {"registry": PAGE_SIZE, "joins": PAGE_SIZE},
        "broken_pages": {},
        "registry": _build_registry(total),
        "joins": _build_joins(total),
    }


def build_big_versions() -> dict[str, dict]:
    return {
        "v-big-20k": _big_version(
            "v-big-20k", 20_000, "2025-06-01T09:00:00", "jsonl",
            "大册 2 万：每页固定 500 条，共 40 页，jsonl 排版"),
        "v-big-80k": _big_version(
            "v-big-80k", 80_000, "2025-09-01T09:00:00", "record",
            "大册 8 万：每页固定 500 条，共 160 页，record 排版"),
    }
