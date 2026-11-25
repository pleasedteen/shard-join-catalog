"""项目自带的固定数据集：整版、更正单与问题版本。

数据完全确定性，不依赖外部资源。整版（kind 缺省为 full）自带：
- registry：陶片登记清单（catalog_no 唯一）
- joins：拼接清单（a/b 为馆藏编号，无向边）
- layouts：两个入口各自的页面排版（pipe / jsonl / record 三种）
- broken_pages：损坏页声明（站点返回标记损坏、校验和不符的页面）

更正单（kind == "correction"）在两个整版之间发布，自带：
- base_version / target_version：基版与目标版编号
- corrections：两个入口各自的更正操作，op 取值 add / upsert / delete，
  只携带发生变化的记录，传输页数与落库规模只随更正规模增长
- 采集端把更正页按目标版编号落盘，激活时只登记谱系（edition_lineage），
  查询时从本地基版整版 + 已确认更正页实时物化目标版。
"""
from __future__ import annotations

VERSIONS: dict[str, dict] = {
    "v1": {
        "published_at": "2024-05-01T09:00:00",
        "note": "首版：9 片陶片，两组拼接",
        "layouts": {"registry": "pipe", "joins": "pipe"},
        "page_size": {"registry": 5, "joins": 3},
        "broken_pages": {},
        "registry": [
            {"catalog_no": "C-001", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 312.5},
            {"catalog_no": "C-002", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 288.0},
            {"catalog_no": "C-003", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 205.2},
            {"catalog_no": "C-004", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 176.4},
            {"catalog_no": "C-005", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 164.0},
            {"catalog_no": "C-006", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 190.7},
            {"catalog_no": "C-007", "museum": "合作馆甲", "material": "彩陶", "weight_g": 98.6},
            {"catalog_no": "C-008", "museum": "合作馆甲", "material": "彩陶", "weight_g": 101.3},
            {"catalog_no": "C-009", "museum": "合作馆甲", "material": "彩陶", "weight_g": 77.9},
        ],
        "joins": [
            {"a": "C-001", "b": "C-002", "bond": "拼接-A 口沿", "confidence": "high"},
            {"a": "C-002", "b": "C-003", "bond": "拼接-A 腹部", "confidence": "high"},
            {"a": "C-004", "b": "C-005", "bond": "拼接-B 口沿", "confidence": "medium"},
            {"a": "C-005", "b": "C-006", "bond": "拼接-B 腹部", "confidence": "medium"},
            {"a": "C-007", "b": "C-008", "bond": "拼接-C 残片", "confidence": "low"},
        ],
    },
    "v2": {
        "published_at": "2024-08-15T10:30:00",
        "note": "第二版：新增 2 片，新增/确认拼接，改用 JSONL 排版",
        "layouts": {"registry": "jsonl", "joins": "jsonl"},
        "page_size": {"registry": 6, "joins": 4},
        "broken_pages": {},
        "registry": [
            {"catalog_no": "C-001", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 310.1},
            {"catalog_no": "C-002", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 286.8},
            {"catalog_no": "C-003", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 205.2},
            {"catalog_no": "C-004", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 176.4},
            {"catalog_no": "C-005", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 164.0},
            {"catalog_no": "C-006", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 190.7},
            {"catalog_no": "C-007", "museum": "合作馆甲", "material": "彩陶", "weight_g": 98.6},
            {"catalog_no": "C-008", "museum": "合作馆甲", "material": "彩陶", "weight_g": 101.3},
            {"catalog_no": "C-009", "museum": "合作馆甲", "material": "彩陶", "weight_g": 77.9},
            {"catalog_no": "C-010", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 240.0},
            {"catalog_no": "C-011", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 155.5},
        ],
        "joins": [
            {"a": "C-001", "b": "C-002", "bond": "拼接-A 口沿", "confidence": "high"},
            {"a": "C-002", "b": "C-003", "bond": "拼接-A 腹部", "confidence": "high"},
            {"a": "C-003", "b": "C-010", "bond": "拼接-A 延伸", "confidence": "medium"},
            {"a": "C-004", "b": "C-005", "bond": "拼接-B 口沿", "confidence": "medium"},
            {"a": "C-005", "b": "C-006", "bond": "拼接-B 腹部", "confidence": "medium"},
            {"a": "C-007", "b": "C-008", "bond": "拼接-C 残片", "confidence": "low"},
        ],
    },
    "v3": {
        "published_at": "2025-01-20T14:00:00",
        "note": "第三版：新增 1 片并入拼接-B，改用 record 排版，含一个损坏页场景",
        "layouts": {"registry": "record", "joins": "record"},
        "page_size": {"registry": 6, "joins": 4},
        # 损坏页可由控制接口动态改写；这里默认完好，验证脚本会现场制造损坏。
        "broken_pages": {},
        "registry": [
            {"catalog_no": "C-001", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 310.1},
            {"catalog_no": "C-002", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 286.8},
            {"catalog_no": "C-003", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 205.2},
            {"catalog_no": "C-004", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 176.4},
            {"catalog_no": "C-005", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 164.0},
            {"catalog_no": "C-006", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 190.7},
            {"catalog_no": "C-007", "museum": "合作馆甲", "material": "彩陶", "weight_g": 98.6},
            {"catalog_no": "C-008", "museum": "合作馆甲", "material": "彩陶", "weight_g": 101.3},
            {"catalog_no": "C-009", "museum": "合作馆甲", "material": "彩陶", "weight_g": 77.9},
            {"catalog_no": "C-010", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 240.0},
            {"catalog_no": "C-011", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 155.5},
            {"catalog_no": "C-012", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 149.8},
        ],
        "joins": [
            {"a": "C-001", "b": "C-002", "bond": "拼接-A 口沿", "confidence": "high"},
            {"a": "C-002", "b": "C-003", "bond": "拼接-A 腹部", "confidence": "high"},
            {"a": "C-003", "b": "C-010", "bond": "拼接-A 延伸", "confidence": "medium"},
            {"a": "C-004", "b": "C-005", "bond": "拼接-B 口沿", "confidence": "medium"},
            {"a": "C-005", "b": "C-006", "bond": "拼接-B 腹部", "confidence": "medium"},
            {"a": "C-006", "b": "C-012", "bond": "拼接-B 新片", "confidence": "medium"},
            {"a": "C-007", "b": "C-008", "bond": "拼接-C 残片", "confidence": "low"},
        ],
    },
    "v4-bad": {
        "published_at": "2025-03-01T08:00:00",
        "note": "问题版本：拼接清单引用了登记中不存在的陶片 X-999，应被引用完整性拦截",
        "layouts": {"registry": "pipe", "joins": "pipe"},
        "page_size": {"registry": 7, "joins": 4},
        "broken_pages": {},
        "registry": [
            {"catalog_no": "C-001", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 310.1},
            {"catalog_no": "C-002", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 286.8},
            {"catalog_no": "C-003", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 205.2},
            {"catalog_no": "C-004", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 176.4},
            {"catalog_no": "C-005", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 164.0},
            {"catalog_no": "C-006", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 190.7},
            {"catalog_no": "C-007", "museum": "合作馆甲", "material": "彩陶", "weight_g": 98.6},
        ],
        "joins": [
            {"a": "C-001", "b": "C-002", "bond": "拼接-A 口沿", "confidence": "high"},
            {"a": "X-999", "b": "C-003", "bond": "引用缺失陶片", "confidence": "low"},
            {"a": "C-004", "b": "C-005", "bond": "拼接-B 口沿", "confidence": "medium"},
        ],
    },
    # ------------------------------------------------------------------
    # 第四个整版：作为更正单的基版（pipe 排版回归，便于演示排版差异）
    # ------------------------------------------------------------------
    "v5": {
        "published_at": "2025-06-10T09:30:00",
        "note": "第四版（整版）：12 片陶片，后续更正单只发变化记录",
        "layouts": {"registry": "pipe", "joins": "pipe"},
        "page_size": {"registry": 6, "joins": 4},
        "broken_pages": {},
        "registry": [
            {"catalog_no": "C-001", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 309.4},
            {"catalog_no": "C-002", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 286.8},
            {"catalog_no": "C-003", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 205.2},
            {"catalog_no": "C-004", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 176.4},
            {"catalog_no": "C-005", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 164.0},
            {"catalog_no": "C-006", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 190.7},
            {"catalog_no": "C-007", "museum": "合作馆甲", "material": "彩陶", "weight_g": 98.6},
            {"catalog_no": "C-008", "museum": "合作馆甲", "material": "彩陶", "weight_g": 101.3},
            {"catalog_no": "C-009", "museum": "合作馆甲", "material": "彩陶", "weight_g": 77.9},
            {"catalog_no": "C-010", "museum": "合作馆甲", "material": "夹砂陶", "weight_g": 240.0},
            {"catalog_no": "C-011", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 155.5},
            {"catalog_no": "C-012", "museum": "合作馆甲", "material": "泥质陶", "weight_g": 149.8},
        ],
        "joins": [
            {"a": "C-001", "b": "C-002", "bond": "拼接-A 口沿", "confidence": "high"},
            {"a": "C-002", "b": "C-003", "bond": "拼接-A 腹部", "confidence": "high"},
            {"a": "C-003", "b": "C-010", "bond": "拼接-A 延伸", "confidence": "medium"},
            {"a": "C-004", "b": "C-005", "bond": "拼接-B 口沿", "confidence": "medium"},
            {"a": "C-005", "b": "C-006", "bond": "拼接-B 腹部", "confidence": "medium"},
            {"a": "C-006", "b": "C-012", "bond": "拼接-B 新片", "confidence": "medium"},
            {"a": "C-007", "b": "C-008", "bond": "拼接-C 残片", "confidence": "low"},
        ],
    },
    # ------------------------------------------------------------------
    # 更正单 1：v5（基版） -> v6（目标版），同时演示新增/改写/删除
    # ------------------------------------------------------------------
    "c1": {
        "kind": "correction",
        "base_version": "v5",
        "target_version": "v6",
        "published_at": "2025-07-01T10:00:00",
        "note": "更正单 v6：新增 C-013、改写 C-001 重量、删除 C-009；拼接新增/改写/删除各一",
        "layouts": {"registry": "jsonl", "joins": "jsonl"},
        "page_size": {"registry": 2, "joins": 2},
        "broken_pages": {},
        "corrections": {
            "registry": [
                # 改写：C-001 已在基版中，按 catalog_no 整体改写
                {"op": "upsert", "catalog_no": "C-001", "museum": "合作馆甲",
                 "material": "夹砂陶", "weight_g": 318.8},
                # 删除：基版中的孤立片 C-009 注销
                {"op": "delete", "catalog_no": "C-009"},
                # 新增：入藏新片 C-013
                {"op": "add", "catalog_no": "C-013", "museum": "合作馆甲",
                 "material": "泥质陶", "weight_g": 132.7},
            ],
            "joins": [
                # 改写：已有边 C-005~C-006 调整粘接说明与置信度
                {"op": "upsert", "a": "C-005", "b": "C-006",
                 "bond": "拼接-B 腹部（复核修正）", "confidence": "high"},
                # 删除：撤销拼接-C
                {"op": "delete", "a": "C-007", "b": "C-008"},
                # 新增：新片 C-013 并入拼接-B
                {"op": "add", "a": "C-012", "b": "C-013",
                 "bond": "拼接-B 新片延伸", "confidence": "medium"},
            ],
        },
    },
    # ------------------------------------------------------------------
    # 更正单 2：v6 -> v7，小更正，演示链式更正与漏版后本地补齐
    # ------------------------------------------------------------------
    "c2": {
        "kind": "correction",
        "base_version": "v6",
        "target_version": "v7",
        "published_at": "2025-07-15T11:00:00",
        "note": "更正单 v7：小更正，改写 C-011 重量并新增一条拼接",
        "layouts": {"registry": "record", "joins": "record"},
        "page_size": {"registry": 1, "joins": 1},
        "broken_pages": {},
        "corrections": {
            "registry": [
                {"op": "upsert", "catalog_no": "C-011", "museum": "合作馆甲",
                 "material": "泥质陶", "weight_g": 160.2},
            ],
            "joins": [
                {"op": "add", "a": "C-011", "b": "C-012",
                 "bond": "拼接-B 口沿延伸", "confidence": "low"},
            ],
        },
    },
    # ------------------------------------------------------------------
    # 问题更正单：拼接新增引用了登记中不存在的陶片，引用完整性必须拦截
    # ------------------------------------------------------------------
    "c-bad-ref": {
        "kind": "correction",
        "base_version": "v6",
        "target_version": "v8-bad",
        "published_at": "2025-08-01T08:00:00",
        "note": "问题更正单：新增拼接引用了不存在的 Y-007，应被拦截且旧版继续可查",
        "layouts": {"registry": "pipe", "joins": "pipe"},
        "page_size": {"registry": 2, "joins": 2},
        "broken_pages": {},
        "corrections": {
            "registry": [
                {"op": "add", "catalog_no": "C-014", "museum": "合作馆甲",
                 "material": "彩陶", "weight_g": 88.0},
            ],
            "joins": [
                {"op": "add", "a": "C-014", "b": "Y-007",
                 "bond": "引用缺失陶片", "confidence": "low"},
            ],
        },
    },
    # ------------------------------------------------------------------
    # 问题更正单：引用的基版本地不存在，无法从本地补齐，必须拦截
    # ------------------------------------------------------------------
    "c-orphan": {
        "kind": "correction",
        "base_version": "v-ghost",
        "target_version": "v9-orphan",
        "published_at": "2025-08-02T08:00:00",
        "note": "问题更正单：基版 v-ghost 本地不存在，无法补齐，应被拦截",
        "layouts": {"registry": "pipe", "joins": "pipe"},
        "page_size": {"registry": 2, "joins": 2},
        "broken_pages": {},
        "corrections": {
            "registry": [
                {"op": "add", "catalog_no": "C-015", "museum": "合作馆甲",
                 "material": "彩陶", "weight_g": 66.0},
            ],
            "joins": [],
        },
    },
}
