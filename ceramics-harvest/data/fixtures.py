"""项目自带的固定数据集：多个合作馆各自的发布版与跨馆拼接登记。

多馆要点（对应需求）：
- 每个馆有自己的版本序列；不同馆可以同时使用相同的版号（v1/v2/v3），互不影响。
- 不同馆可以登记相同的馆藏编号（例如甲、乙都有 C-001），编号只在馆内唯一。
- 跨馆拼接登记写在各自馆的 joins 里，用 "馆名:馆藏编号" 指向对方，例如
  甲馆 joins 中的 {"a": "C-007", "b": "yi:C-001"}，乙馆用 {"a": "C-001", "b": "jia:C-007"}
  登记另一端。两端都处于已发布状态时该跨馆拼接自动生效；任一端新版不再登记，
  立即退出查询结果。
- 乙馆 v1 尚未登记跨馆拼接、v2 才登记（跨馆登记晚到），v3 又撤下该登记。

排版仍按版本变化（pipe / jsonl / record），采集端据每页 layout 解析。
"""
from __future__ import annotations

SITES: dict[str, dict] = {
    "jia": {
        "name": "合作馆甲",
        "initial_version": "v1",
        "versions": {
            "v1": {
                "published_at": "2024-05-01T09:00:00",
                "note": "首版：9 片陶片，两组拼接，并登记一条跨馆拼接的一端",
                "layouts": {"registry": "pipe", "joins": "pipe"},
                "page_size": {"registry": 5, "joins": 3},
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
                    {"a": "C-007", "b": "yi:C-001", "bond": "跨馆-A 接缝", "confidence": "medium"},
                ],
            },
            "v2": {
                "published_at": "2024-08-15T10:30:00",
                "note": "第二版：新增 2 片，新增/确认拼接，改用 JSONL 排版",
                "layouts": {"registry": "jsonl", "joins": "jsonl"},
                "page_size": {"registry": 6, "joins": 4},
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
                    {"a": "C-007", "b": "yi:C-001", "bond": "跨馆-A 接缝", "confidence": "medium"},
                ],
            },
            "v3": {
                "published_at": "2025-01-20T14:00:00",
                "note": "第三版：新增 1 片并入拼接-B，改用 record 排版",
                "layouts": {"registry": "record", "joins": "record"},
                "page_size": {"registry": 6, "joins": 4},
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
                    {"a": "C-007", "b": "yi:C-001", "bond": "跨馆-A 接缝", "confidence": "medium"},
                ],
            },
            "v4-bad": {
                "published_at": "2025-03-01T08:00:00",
                "note": "问题版本：拼接清单引用登记中不存在的 X-999，应被引用完整性拦截",
                "layouts": {"registry": "pipe", "joins": "pipe"},
                "page_size": {"registry": 7, "joins": 4},
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
        },
    },
    "yi": {
        "name": "合作馆乙",
        "initial_version": "v1",
        "versions": {
            "v1": {
                "published_at": "2024-06-02T09:00:00",
                "note": "乙馆首版：与甲馆重号的 C-001，尚未登记跨馆拼接另一端",
                "layouts": {"registry": "pipe", "joins": "pipe"},
                "page_size": {"registry": 3, "joins": 2},
                "registry": [
                    {"catalog_no": "C-001", "museum": "合作馆乙", "material": "黑陶", "weight_g": 201.0},
                    {"catalog_no": "C-002", "museum": "合作馆乙", "material": "黑陶", "weight_g": 188.4},
                    {"catalog_no": "C-003", "museum": "合作馆乙", "material": "灰陶", "weight_g": 143.2},
                ],
                "joins": [
                    {"a": "C-001", "b": "C-002", "bond": "乙拼接-1", "confidence": "high"},
                ],
            },
            "v2": {
                "published_at": "2024-09-10T11:00:00",
                "note": "乙馆第二版：新增 C-004，并晚到地登记跨馆拼接另一端（甲:C-007）",
                "layouts": {"registry": "jsonl", "joins": "jsonl"},
                "page_size": {"registry": 3, "joins": 2},
                "registry": [
                    {"catalog_no": "C-001", "museum": "合作馆乙", "material": "黑陶", "weight_g": 201.0},
                    {"catalog_no": "C-002", "museum": "合作馆乙", "material": "黑陶", "weight_g": 188.4},
                    {"catalog_no": "C-003", "museum": "合作馆乙", "material": "灰陶", "weight_g": 143.2},
                    {"catalog_no": "C-004", "museum": "合作馆乙", "material": "灰陶", "weight_g": 132.0},
                ],
                "joins": [
                    {"a": "C-001", "b": "C-002", "bond": "乙拼接-1", "confidence": "high"},
                    {"a": "C-003", "b": "C-004", "bond": "乙拼接-2", "confidence": "medium"},
                    {"a": "C-001", "b": "jia:C-007", "bond": "跨馆-A 接缝", "confidence": "medium"},
                ],
            },
            "v3": {
                "published_at": "2025-02-01T09:30:00",
                "note": "乙馆第三版：撤下跨馆拼接登记，馆内数据改用 record 排版",
                "layouts": {"registry": "record", "joins": "record"},
                "page_size": {"registry": 3, "joins": 2},
                "registry": [
                    {"catalog_no": "C-001", "museum": "合作馆乙", "material": "黑陶", "weight_g": 201.0},
                    {"catalog_no": "C-002", "museum": "合作馆乙", "material": "黑陶", "weight_g": 188.4},
                    {"catalog_no": "C-003", "museum": "合作馆乙", "material": "灰陶", "weight_g": 143.2},
                    {"catalog_no": "C-004", "museum": "合作馆乙", "material": "灰陶", "weight_g": 132.0},
                ],
                "joins": [
                    {"a": "C-001", "b": "C-002", "bond": "乙拼接-1", "confidence": "high"},
                    {"a": "C-003", "b": "C-004", "bond": "乙拼接-2", "confidence": "medium"},
                ],
            },
        },
    },
    "bing": {
        "name": "合作馆丙",
        "initial_version": "v1",
        "versions": {
            "v1": {
                "published_at": "2024-07-01T08:00:00",
                "note": "丙馆首版：两片一片",
                "layouts": {"registry": "pipe", "joins": "pipe"},
                "page_size": {"registry": 2, "joins": 2},
                "registry": [
                    {"catalog_no": "P-001", "museum": "合作馆丙", "material": "白陶", "weight_g": 120.0},
                    {"catalog_no": "P-002", "museum": "合作馆丙", "material": "白陶", "weight_g": 110.0},
                ],
                "joins": [
                    {"a": "P-001", "b": "P-002", "bond": "丙拼接-1", "confidence": "high"},
                ],
            },
            "v2": {
                "published_at": "2024-10-01T08:00:00",
                "note": "丙馆第二版：新增 P-003，登记拆成多页以便观察暂停后的断点续采",
                "layouts": {"registry": "pipe", "joins": "pipe"},
                "page_size": {"registry": 2, "joins": 2},
                "registry": [
                    {"catalog_no": "P-001", "museum": "合作馆丙", "material": "白陶", "weight_g": 120.0},
                    {"catalog_no": "P-002", "museum": "合作馆丙", "material": "白陶", "weight_g": 110.0},
                    {"catalog_no": "P-003", "museum": "合作馆丙", "material": "白陶", "weight_g": 99.0},
                ],
                "joins": [
                    {"a": "P-001", "b": "P-002", "bond": "丙拼接-1", "confidence": "high"},
                    {"a": "P-002", "b": "P-003", "bond": "丙拼接-2", "confidence": "medium"},
                ],
            },
        },
    },
}

# 兼容旧接口：默认馆（甲馆）的数据集
VERSIONS = SITES["jia"]["versions"]
SITE_NAMES = {site: spec["name"] for site, spec in SITES.items()}
