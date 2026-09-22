"""灰度边界、分组稳定性与最小重分组测试。"""

from __future__ import annotations

import logging

from app.rollout import MAX_BUCKET, evaluate_percentage, stable_bucket

logger = logging.getLogger("tests")


def test_percentage_boundaries_zero_and_hundred():
    # 0% 全关、100% 全开，包含桶边界
    assert evaluate_percentage(0, 0) is False
    assert evaluate_percentage(MAX_BUCKET - 1, 0) is False
    assert evaluate_percentage(0, 100) is True
    assert evaluate_percentage(MAX_BUCKET - 1, 100) is True
    # 1% 的半开区间边界
    assert evaluate_percentage(99, 1) is True
    assert evaluate_percentage(100, 1) is False
    logger.info("boundary MAX_BUCKET=%s 1%% threshold=%s", MAX_BUCKET, MAX_BUCKET // 100)


def test_grouping_is_reproducible_for_same_subject_and_salt():
    b = stable_bucket("user-007", "flag-a")
    for _ in range(50):
        assert stable_bucket("user-007", "flag-a") == b


def test_different_salts_differentiate_flags():
    # 同一主体在不同开关（不同 salt）下不共享分桶结果
    salts = {salt: stable_bucket("user-007", salt) for salt in ("a", "b", "c")}
    assert len(set(salts.values())) >= 2  # 极大概率互不相同


def test_percentage_distribution_is_approximately_accurate():
    subjects = [f"subject-{i}" for i in range(30_000)]
    for pct in (1, 10, 25, 50, 90):
        on = sum(evaluate_percentage(stable_bucket(s, "f"), pct) for s in subjects)
        ratio = on / len(subjects)
        logger.info("distribution percentage=%s observed=%.4f", pct, ratio)
        assert abs(ratio - pct / 100) < 0.01


def test_adjusting_percentage_causes_minimal_re_grouping():
    # 关键稳定性：提高比例不应大面积重新分组
    subjects = [f"u{i}" for i in range(10_000)]
    old = {s: evaluate_percentage(stable_bucket(s, "f"), 10) for s in subjects}
    new = {s: evaluate_percentage(stable_bucket(s, "f"), 15) for s in subjects}
    changed = [s for s in subjects if old[s] != new[s]]
    # 只有从 off 变 on 的（约 5%），没有 on 变回 off
    assert all(old[s] is False and new[s] is True for s in changed)
    assert 0.03 < len(changed) / len(subjects) < 0.07
    logger.info("re-group 10%%->15%% changed=%d/%d", len(changed), len(subjects))

    # 再降回去，归属完全恢复（无状态漂移）
    back = {s: evaluate_percentage(stable_bucket(s, "f"), 10) for s in subjects}
    assert back == old


def test_manager_decide_is_stable_across_calls_but_versioned():
    from app.manager import ConfigManager

    settings = {
        "service_name": "svc", "max_connections": 100, "request_timeout_ms": 100,
        "api_base_url": "u", "secret_token": "k",
    }
    m = ConfigManager()
    m.load_initial({"settings": settings,
                    "flags": {"f": {"enabled": True, "rollout": {"percentage": 30}}}})
    v1 = {s: m.decide("f", s)[0] for s in (f"user-{i}" for i in range(500))}
    v1_reasons = {s: m.decide("f", s)[1] for s in ("user-0",)}
    assert all("version=1" in r for r in v1_reasons.values())
    # 同版本重复判定完全一致
    assert {s: m.decide("f", s)[0] for s in v1} == v1
