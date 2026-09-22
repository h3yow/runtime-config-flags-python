# 分层配置与特性开关子系统

在既有 FastAPI 服务（`GET /` 行为不变）之上，提供运行期可安全变更的分层配置与按比例灰度开关。

## 配置来源与优先级

三层来源按以下顺序合并，**后者覆盖前者**（字段级深合并）：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1（最低） | schema 默认值 | `ConfigSchema` 中 `FieldSpec.default` / `FlagSpec.default` |
| 2 | 配置文件 | JSON 文件，路径由 `APP_CONFIG_FILE` 指定（默认 `config.json`），不存在视为空层 |
| 3（最高） | 环境变量 | 前缀 `APP_`：`APP_FIELD_<KEY>`、`APP_FLAG_<NAME>=true|false`、`APP_ROLLOUT_<NAME>=0-100` |

配置文件支持两种写法（可混用）：

```json
{
  "fields": {"limit": 50},
  "flags": {"new_ui": {"enabled": true, "percentage": 30}, "beta": true},
  "greeting": "hi"
}
```

### 校验与拒绝原因

合并后的整体视图按 schema 校验，**任何非法配置都不会静默回退**，而是抛出携带可区分原因的 `ConfigRejectionError`：

| reason | 含义 |
| --- | --- |
| `missing_required` | 必填字段缺失 |
| `type_mismatch` | 类型不符（`bool` 不会被当作 `int`） |
| `illegal_value` | 越界、不在枚举内、灰度比例不在 0-100 |
| `invalid_key` | 未在 schema 声明的字段/开关 |
| `source_read_error` | 配置文件无法读取或不是合法 JSON |

校验失败时**继续沿用上一个可用版本**，服务不中断、接口行为不变。

## 更新与回滚语义

- **原子可见**：配置以不可变快照（`ConfigSnapshot`，版本号为内容哈希）整体替换。任一请求在处理全程只观察到完整的旧版本或新版本，绝不会看到半新半旧的中间态。
- **热更新**：`POST /config/reload` 重新读取三层来源并校验；成功则原子切换，失败返回 `422` 及 `reason/path/detail/serving_version`，旧版本继续服役。
- **回滚**：`POST /config/rollback` 恢复到上一个可用版本（管理器保留一代历史）。
- **并发安全**：快照读写均在锁内完成；快照本身不可变，读者无需拷贝。
- **密钥保护**：`secret` 类型字段在任何对外输出（`GET /config`、日志视图）中一律显示为 `***REDACTED***`，仅服务内部 `snapshot.get()` 可取原值。

## 灰度分组规则

开关可设 `percentage`（0-100）进入灰度模式，分组规则：

```
bucket = sha256("{salt}|{flag}|{subject}")[:16] % 100   # 0-99 共 100 桶
开启  <=>  bucket < percentage
```

- **稳定可复现**：同一主体在同一配置版本下判定恒定；盐（salt）为部署级常量、与版本号解耦，因此修改无关字段不会导致分组漂移。
- **比例平滑**：按“桶位 < 比例”的前缀判定，比例从 p1 调大到 p2 时仅新增桶位 `[p1, p2)` 的主体，已开启者不被踢出，无大面积重新分组。
- **可解释**：`GET /flags/{name}?subject=xxx` 返回 `version / enabled / reason / bucket / percentage`，`reason` 取值：`boolean_on`、`boolean_off`、`rollout_in`、`rollout_out`、`unknown_flag`。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 既有接口，行为不变 |
| GET | `/config` | 当前生效配置（密钥脱敏） |
| GET | `/config/status` | 当前/上一版本号与最近一次拒绝原因 |
| POST | `/config/reload` | 热更新；失败 422 + 可区分原因 |
| POST | `/config/rollback` | 回滚到上一可用版本 |
| GET | `/flags/{name}?subject=` | 开关判定（含版本与判定依据） |

## 本地验证

```bash
uv sync                      # 安装依赖
uv run uvicorn main:app --reload   # 启动服务

# 运行全部测试（-s 可看到测试日志打印的配置版本与判定依据）
uv run pytest -s

# 手动验证热更新
echo '{"fields": {"limit": 50}, "flags": {"new_ui": {"enabled": true, "percentage": 30}}}' > config.json
curl -X POST localhost:8000/config/reload
curl localhost:8000/config
curl 'localhost:8000/flags/new_ui?subject=user-1'

# 验证失败不生效：写入非法配置后 reload 返回 422，/config 仍是旧版本
echo '{"fields": {"limit": "bad"}}' > config.json
curl -X POST localhost:8000/config/reload   # 422 type_mismatch
curl localhost:8000/config/status           # 查看 last_rejection
```

测试覆盖：热更新失败保留旧版本、各类非法配置原因可区分、并发读写原子性、灰度边界（0/100/桶位边界）、分组稳定性与比例平滑扩张、密钥脱敏。
