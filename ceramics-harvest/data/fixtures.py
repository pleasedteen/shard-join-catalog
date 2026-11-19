"""项目自带的固定数据集：三个可正常发布的版本 + 一个引用不完整的版本。

数据完全确定性，不依赖外部资源。每个版本自带：
- registry：陶片登记清单（catalog_no 唯一）
- joins：拼接清单（a/b 为馆藏编号，无向边）
- layouts：两个入口各自的页面排版（pipe / jsonl / record 三种）
- broken_pages：损坏页声明（站点返回标记损坏、校验和不符的页面）
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
}
