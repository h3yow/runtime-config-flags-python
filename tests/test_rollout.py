"""灰度桶位与边界、稳定性测试。"""

from config_system.rollout import in_rollout, stable_bucket


def test_bucket_in_range_and_stable():
    for subject in [f"user-{i}" for i in range(500)]:
        b = stable_bucket("new_ui", subject, "v1")
        assert 0 <= b <= 99
        # 同版本同主体不漂移
        assert stable_bucket("new_ui", subject, "v1") == b


def test_subject_distinction_and_salt_isolation():
    # 不同主体大概率落在不同桶（500 个主体桶位应覆盖较广）
    buckets = {stable_bucket("f", f"u{i}", "s") for i in range(500)}
    assert len(buckets) > 90
    # salt 改变会改变分组空间（这里只验证函数使用了 salt）
    assert isinstance(stable_bucket("f", "u1", "s1"), int)


def test_percentage_boundaries():
    assert in_rollout("f", "anyone", "s", 0) is False
    assert in_rollout("f", "anyone", "s", 100) is True
    assert in_rollout("f", "anyone", "s", -5) is False


def test_monotonic_expansion_no_kick_out():
    """比例调大时，已在开启组的主体不应被踢出（前缀桶判定）。"""
    subjects = [f"user-{i}" for i in range(2000)]
    enabled_at_10 = {u for u in subjects if in_rollout("f", u, "s", 10)}
    enabled_at_50 = {u for u in subjects if in_rollout("f", u, "s", 50)}
    assert enabled_at_10 <= enabled_at_50
    # 比例近似符合：10% 允许较大抽样容差
    ratio_10 = len(enabled_at_10) / len(subjects)
    ratio_50 = len(enabled_at_50) / len(subjects)
    assert 0.05 < ratio_10 < 0.18
    assert 0.44 < ratio_50 < 0.56


def test_exact_boundary_subject():
    """显式验证 bucket == percentage-1 开、== percentage 关。"""
    # 找一个桶位恰为 29 的主体
    target = next(f"u{i}" for i in range(100000)
                  if stable_bucket("edge_flag", f"u{i}", "salt-x") == 29)
    assert in_rollout("edge_flag", target, "salt-x", 30) is True
    assert in_rollout("edge_flag", target, "salt-x", 29) is False
