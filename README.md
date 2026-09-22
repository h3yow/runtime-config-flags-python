# 运行期分层配置与特性开关子系统

在原有 FastAPI 服务（`GET /` 行为保持不变）之上，提供一套可在运行期安全变更、
按配置版本给出一致且可解释结果的配置 / 特性开关子系统。

## 能力概览

- **分层合并**：内置默认值 < 配置文件（JSON）< 环境变量，优先级固定、可溯源。
- **强校验不静默回退**：类型不符、非法取值、缺少必填项都会被拒绝，
  原因按 `MISSING_REQUIRED` / `TYPE_MISMATCH` / `INVALID_VALUE` / `SOURCE_ERROR` 分类。
- **原子热更新**：更新先完整校验、再整引用替换；失败不部分生效，
  服务继续沿用上一个可用版本，接口行为不变。
- **按比例灰度**：基于 SHA-256 的稳定分桶，同一主体在同一版本下归属恒定，
  调整比例只影响跨边界的少量主体。
- **密钥脱敏**：`secret_token` 在所有对外输出中固定为 `***redacted***`。

## 配置来源与优先级

优先级从低到高（高层覆盖低层的同名叶子键）：

| 优先级 | 来源 | 类 | 说明 |
| --- | --- | --- | --- |
| 1（最低） | 内置默认值 | `DefaultsSource` | `app/bootstrap.py` 中的完整可用默认配置 |
| 2 | 配置文件 | `FileSource` | 路径取 `APP_CONFIG_FILE`，默认 `config.json`（默认路径缺失按空层处理；显式指定路径缺失则拒绝） |
| 3（最高） | 环境变量 | `EnvironmentSource` | 前缀 `APP_`，`__` 表示嵌套 |

合并为递归深合并（仅当两侧同为对象时下钻，列表整体替换），
合并结果附带 `provenance`，记录每个最终值来自哪一层，便于解释。

环境变量命名约定（值会先尝试按 JSON 解析，失败则按字符串保留）：

```
APP_SETTINGS__MAX_CONNECTIONS=200
APP_SETTINGS__ENABLE_CACHE=true          # -> bool
APP_FLAGS__NEW_CHECKOUT__ROLLOUT__PERCENTAGE=30
```

### 配置结构

```json
{
  "settings": {
    "service_name": "demo-service",
    "max_connections": 100,
    "request_timeout_ms": 2000,
    "enable_cache": true,
    "api_base_url": "https://api.example.com",
    "secret_token": "change-me"
  },
  "flags": {
    "new_checkout": {
      "enabled": true,
      "rollout": { "percentage": 25, "salt": "new_checkout" }
    },
    "legacy_mode": { "enabled": false }
  }
}
```

- `settings.max_connections` ∈ [1, 100000]；`request_timeout_ms` ∈ [1, 600000]。
- `rollout.percentage` ∈ [0, 100]；`salt` 可省略，缺省取开关名。
- 布尔字段不接受 `1`/`"true"` 之类的隐式转换（环境变量除外，其按 JSON 解析）。

## 更新与回滚语义

- 每个版本是一个**不可变** `ConfigSnapshot`（`version` 从 1 递增）。
- 启动时加载初始版本；初始配置非法则直接启动失败，绝不带错运行。
- `POST /admin/reload` 触发热更新：
  - 成功：整体替换为新版本，返回新版本号；
  - 失败：返回 `422` 与可区分的 `reasons`，同时返回 `kept_version`，
    旧版本**原样保留**，不会出现"一半新值一半旧值"。
- "回滚"即把配置来源恢复到旧内容后再 reload（旧快照一直保留到被成功替换为止）；
  失败的更新本身不会推进版本号，因此无需额外回滚动作。
- 并发模型：管理器只持有一个快照引用，请求处理开始时取一次引用，
  整次判定（含响应中的 `version` 与 `reason`）都基于该引用，
  只能观察到完整的旧版本或新版本。

### 拒绝原因分类

| 前缀 | 含义 |
| --- | --- |
| `SOURCE_ERROR: CONFIG_FILE_NOT_FOUND / INVALID_JSON / ...` | 来源读取失败 |
| `MISSING_REQUIRED` | 缺少必填项 |
| `TYPE_MISMATCH` | 类型不符（含布尔/整数混用） |
| `INVALID_VALUE` | 类型正确但取值越界或为空 |

一次校验会收集全部问题，而非遇到第一个就停止。

## 灰度分组规则

- 桶空间为 10000 桶：`bucket = SHA256(salt + "\x1f" + subject) % 10000`。
- 命中条件：`bucket < percentage * 100`（半开区间）。
  `percentage=0` 全关，`=100` 全开，1% 对应桶 `0..99`。
- **稳定可复现**：判定是纯函数，同一 `(subject, salt, percentage)` 结果恒定。
- **最小重分组**：调整 `percentage` 只改变跨边界的主体，
  如 10%→15% 仅约 5% 主体从 off 变 on，没有 on→off 抖动；调回去完全恢复。
- 不带 `subject` 访问灰度开关时返回 off，依据为 `subject_required_for_rollout`。

## HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /` | 原有接口，行为不变：`{"Hello": "World"}` |
| `GET /config` | 当前版本的脱敏视图（版本号、配置、开关） |
| `GET /flags/{name}?subject=...` | 开关判定，响应含 `decision/version/reason`，并打印同样内容到日志 |
| `POST /admin/reload` | 热更新；成功返回新版本，失败返回 422 与分类原因 |

判定依据示例（响应与日志一致）：

```
version=3 flag=new_checkout reason=rollout_bucket subject=alice salt=new_checkout
percentage=25 bucket=3317 threshold=2500 hit=false action=off
```

## 本地验证

```bash
# 安装依赖（需要 uv）
uv sync
uv add --dev pytest        # 若尚未安装测试依赖

# 直接启动（默认读取 ./config.json；缺失时使用内置默认值）
uv run uvicorn main:app --reload
# 然后：
curl http://127.0.0.1:8000/config
curl 'http://127.0.0.1:8000/flags/new_checkout?subject=alice'
curl -X POST http://127.0.0.1:8000/admin/reload

# 用环境变量覆盖某项（最高优先级）
APP_SETTINGS__MAX_CONNECTIONS=500 uv run uvicorn main:app

# 运行全部测试（日志打印配置版本与判定依据）
uv run pytest -s -q
```

测试覆盖（28 个用例）：

- 分层优先级与来源溯源；三类校验拒绝原因可区分；多问题一次报全。
- 灰度 0%/100%/1% 边界、分布近似准确、同主体可复现、调比例最小重分组。
- 热更新失败整体拒绝、旧版本保留、无部分生效；来源错误 vs 校验错误可区分。
- 多线程并发读写期间每个请求只看到自洽的完整版本；同版本判定依据唯一。
- 密钥在任何对外输出中均不出现明文。

## 代码结构

```
app/
  errors.py      # 错误类型（校验/来源/更新拒绝）
  model.py       # Settings / FeatureFlag / ConfigSnapshot 等不可变模型
  sources.py     # Defaults / File / Environment 三个来源
  merge.py       # 分层深合并与来源溯源
  validation.py  # 强类型校验，产出分类拒绝原因
  rollout.py     # 稳定哈希分桶
  manager.py     # 线程安全的版本持有者、判定、脱敏视图
  service.py     # 组装 来源->合并->校验->原子发布
  bootstrap.py   # 默认分层组装
  api.py         # HTTP 路由
main.py          # FastAPI 应用（lifespan 内加载初始版本）
tests/           # 上述全部测试
```
