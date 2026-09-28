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

## 强制名单与整体关闭

每个开关除 `enabled` / `percentage` 外还支持三个可选配置：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `global_off` | bool | 一键全量关。`true` 时对**所有主体**一律关闭 |
| `force_on` | string[] | 强制打开名单，命中即开，不受灰度比例影响 |
| `force_off` | string[] | 强制关闭名单，命中即关，不受灰度比例影响 |

### 判定优先级（高 -> 低）

```
1. global_off 整体关闭     -> enabled=false, reason=global_off   （优先级最高，压过一切）
2. force_off 强制关闭名单  -> enabled=false, reason=force_off
3. force_on  强制打开名单  -> enabled=true,  reason=force_on
4. 比例灰度 / 纯布尔开关    -> rollout_in / rollout_out / boolean_on / boolean_off
```

- **同时命中两个名单时以关闭为准**（`force_off` 先于 `force_on` 判定），结果确定，不会忽开忽关。
- **名单主体稳定命中**：名单命中走第 2/3 条短路，与比例完全无关；比例从 0 调到 100 都不改变名单结果。
- **不搅乱名单外主体**：名单外主体继续走第 4 条比例灰度；增加名单不改变桶位计算，比例调大时名单外主体仍然“只能进不能出”（前缀桶特性保持不变）。
- **边界确定性**：比例为 0 或 100 时规则依旧自洽——未整体关闭时 0% 恒为 `rollout_out`、100% 恒为 `rollout_in`；一旦 `global_off=true`，无论 0%、100% 还是名单中有人，一律 `global_off`。
- 纯布尔开关（未设 `percentage`）同样适用上述优先级：`global_off` 与名单都可压过 `enabled=true`。

配置示例：

```json
{
  "flags": {
    "new_ui": {
      "enabled": true,
      "percentage": 30,
      "global_off": false,
      "force_on": ["vip-001", "vip-002"],
      "force_off": ["blocked-007"]
    }
  }
}
```

### 名单校验与原子性

名单与 `global_off` 与其他配置走同一套整体校验，任何一处非法都会拒绝**整次更新**，服务继续用上一个好版本，不会只生效一半：

- `force_on` / `force_off` 必须是字符串数组（标量、对象等整体拒绝，`type_mismatch`，路径如 `flags.new_ui.force_on`）；
- 数组内每个元素必须是字符串，定位到具体下标（如 `flags.new_ui.force_off[0]`）；重复元素自动去重，合法；
- `global_off` 必须是布尔量；
- 在未声明的开关下写名单（或任何字段）按 `invalid_key` 拒绝。

> 注：环境变量层只支持标量覆盖（`APP_FLAG_*` / `APP_ROLLOUT_*`），名单与 `global_off` 请通过配置文件下发；三层深合并时文件层给出的名单整体替换默认空名单。

## 批量判定

`POST /flags/{name}/batch` 一次请求判定一批主体：

```json
// 请求
{"subjects": ["user-1", "vip-001", "blocked-007", "user-1"]}

// 响应（节选）
{
  "version": "2f049087427c",
  "flag": "new_ui",
  "count": 4,
  "results": [
    {"version": "2f049087427c", "flag": "new_ui", "subject": "user-1",
     "enabled": false, "reason": "rollout_out", "bucket": 73, "percentage": 30},
    {"version": "...", "subject": "vip-001", "enabled": true, "reason": "force_on"}
  ]
}
```

- **版本一致性**：每批在处理开始时取一次不可变快照（`mgr.current()`），整批复用同一快照；响应顶层与每个结果都带同一个 `version`。即使判定过程中其他线程热更新，本批也绝不可能一半旧版本、一半新版本。
- **判定依据**：每个主体都有独立 `reason`（`rollout_in/out`、`force_on`、`force_off`、`global_off`、`boolean_on/off`、`unknown_flag`），可直接对账。
- **空批次**：`{"subjects": []}` 合法，返回 `count=0`、空 `results`，并仍带当前 `version`。
- **重复主体**：按输入顺序逐位保留，重复位的判定结果必然一致（同一快照下主体是确定映射）。
- **错误形状**：`subjects` 必须是字符串数组；标量、缺失字段、非字符串元素返回 `422`。
- **规模**：判定为纯内存哈希计算，单主体 O(1)、整批 O(n)，无数据库/IO；单批几千主体为亚秒级，单位均摊耗时不随批量增长（测试覆盖 1k/4k 两档）。

## 兼容性

- **老配置文件无需改动**：`global_off` 默认 `false`、两个名单默认空，老配置（只写 `enabled` / `percentage`）构建出的版本行为与旧版完全一致；`force_on/off` 命中短路只影响名单内主体。
- **老接口与字段不变**：`GET /flags/{name}` 的原有字段（`version/enabled/reason/bucket/percentage/subject`）保持不变；单主体判定结果中新增统一回显 `subject`（缺省主体按 `""` 处理），布尔判定也带该字段。
- **新增接口**：`POST /flags/{name}/batch` 为纯新增，不影响既有路由；`GET /` 行为依旧不变。
- **`GET /config` 视图扩展**：每个 flag 额外输出 `global_off / force_on / force_off`（排序后的名单），原有字段保留。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 既有接口，行为不变 |
| GET | `/config` | 当前生效配置（密钥脱敏） |
| GET | `/config/status` | 当前/上一版本号与最近一次拒绝原因 |
| POST | `/config/reload` | 热更新；失败 422 + 可区分原因 |
| POST | `/config/rollback` | 回滚到上一可用版本 |
| GET | `/flags/{name}?subject=` | 开关判定（含版本与判定依据） |
| POST | `/flags/{name}/batch` | 批量判定，body `{"subjects": [...]}`，整批同版本 |

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

# 验证名单与整体关闭（同时在两个名单里的 both 应返回 force_off）
cat > config.json <<'JSON'
{"flags": {"new_ui": {"enabled": true, "percentage": 30,
  "force_on": ["vip-001", "both"],
  "force_off": ["both"], "global_off": false}}}
JSON
curl -X POST localhost:8000/config/reload
curl 'localhost:8000/flags/new_ui?subject=vip-001'   # reason=force_on
curl 'localhost:8000/flags/new_ui?subject=both'      # reason=force_off（两名单冲突以关为准）

# 一键全量关：把 global_off 改成 true 后，vip-001 也变为 reason=global_off
curl 'localhost:8000/flags/new_ui?subject=vip-001'

# 批量判定：注意顶层 version 与每条结果 version 一致
curl -s -X POST localhost:8000/flags/new_ui/batch \
  -H 'content-type: application/json' \
  -d '{"subjects": ["user-1", "vip-001", "both", "user-1"]}' | python -m json.tool
curl -s -X POST localhost:8000/flags/new_ui/batch \
  -H 'content-type: application/json' -d '{"subjects": []}' | python -m json.tool

# 名单写坏：force_on 写成字符串（非数组），reload 返回 422，旧版本继续服役
echo '{"flags": {"new_ui": {"enabled": true, "force_on": "vip-001"}}}' > config.json
curl -X POST localhost:8000/config/reload   # 422 type_mismatch, flags.new_ui.force_on
curl localhost:8000/config/status           # 查看 last_rejection

# 验证失败不生效：写入非法配置后 reload 返回 422，/config 仍是旧版本
echo '{"fields": {"limit": "bad"}}' > config.json
curl -X POST localhost:8000/config/reload   # 422 type_mismatch
curl localhost:8000/config/status           # 查看 last_rejection
```

测试覆盖：热更新失败保留旧版本、各类非法配置原因可区分、并发读写原子性、灰度边界（0/100/桶位边界）、分组稳定性与比例平滑扩张、密钥脱敏；
新增覆盖整体关闭与比例/名单叠加、两名单同时命中以关为准、比例调整后名单外主体不被踢出、名单/global_off 类型错误原子拒绝、批量判定整批同版本、批量与热更新并发不串版本、空批次/重复主体一致处理、1k/4k 规模耗时。测试以 `-s` 运行时会打印每次判定/每批的版本号与判定依据（reason），出问题可直接对账。
