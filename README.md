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
- **可解释**：`GET /flags/{name}?subject=xxx` 返回 `version / enabled / reason / bucket / percentage`。
- **纯布尔开关**：`percentage` 缺省（-1）时按 `enabled` 整体开/关，`reason` 为 `boolean_on / boolean_off`；同样受整体关闭与强制名单约束（见下节）。

## 强制名单与一键全量关

每个开关（**含未设比例的纯布尔开关**）在 `enabled + percentage` 之外还支持三个字段（均可选，老配置缺省行为不变）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `force_on` | `string[]` | 强制打开名单：命中即开，与比例和 `enabled` 无关 |
| `force_off` | `string[]` | 强制关闭名单：命中即关，与比例和 `enabled` 无关 |
| `kill_switch` | `bool` | 一键全量关：`true` 时对所有主体一律关闭 |

所有开关（纯布尔与灰度）判定优先级**从高到低**固定为：

```
1. kill_switch 整体关闭   -> enabled=false, reason=kill_switch   （忽略 enabled、比例与名单）
2. force_off 强制关闭名单 -> enabled=false, reason=force_off
3. force_on  强制打开名单 -> enabled=true,  reason=force_on
4. 纯布尔开关（未设比例） -> reason=boolean_on / boolean_off
5. 稳定桶位比例判定       -> reason=rollout_in / rollout_out
```

由此保证的确定性语义：

- **整体关闭对所有开关生效**：包括没设比例的纯布尔开关。即使 `percentage=100`、`enabled=true` 且主体在 `force_on` 名单里，只要 `kill_switch=true` 一律关闭，且 `reason` 明确为 `kill_switch`，不会与「开关本来就是关的」（`boolean_off`）或「比例没选中」（`rollout_out`）混淆；解除后恢复开关原本的开/关状态或比例判定。
- **名单对所有开关生效**：名单命中与是否设比例无关——纯布尔开关上 `force_on` 能打开 `enabled=false` 的开关，`force_off` 能关掉 `enabled=true` 的开关。
- **冲突以关闭为准**：主体同时出现在两个名单里时结果恒为 `force_off`，不会反复横跳。
- **名单稳定命中**：名单主体不受比例调整影响；比例调大只按前缀桶放入新主体，名单外已开启者不会被踢出。
- **边界确定**：`percentage=0`（无名单、未整体关）对普通主体恒为 `rollout_out`，`percentage=100` 恒为 `rollout_in`；叠加 `kill_switch` / 名单后按上表优先级得到唯一结果。

名单本身写坏时（名单不是数组、元素不是字符串、`kill_switch` 不是布尔值、或写到了未声明的开关上），整次更新以 `422 type_mismatch` / `invalid_key` 拒绝，服务继续沿用上一个好版本，不会只生效一半。

```json
{
  "flags": {
    "new_ui": {
      "enabled": true,
      "percentage": 30,
      "force_on": ["vip-001", "vip-002"],
      "force_off": ["blocked-007"],
      "kill_switch": false
    }
  }
}
```

## 批量判定

`POST /flags/{name}/batch`，请求体 `{"subjects": ["user-1", "user-2", ...]}`：

- **版本一致性**：请求进入处理时只取一次当前不可变快照，整批基于同一配置版本出结果；返回顶层与每条结果都带同一个 `version`，并给出该主体命中的规则（`rollout_in / rollout_out / force_on / force_off / kill_switch / unknown_flag`）。处理期间即使发生热更新，这一批也不会一半旧版本一半新版本。
- **空批次**：`subjects: []` 返回 `200` 与空 `results`（顶层仍带当前版本号）。
- **重复主体**：按入参顺序为每次出现各返回一条，同一主体的结果必然一致；桶位按主体缓存，重复主体不会重复计算。
- **规模**：单次哈希仅一次 SHA-256，判定为集合成员判断，5000 主体为毫秒级；重复主体越多缓存收益越大，耗时随主体数近似线性，不会越批量越慢得离谱。
- **非法入参**：`subjects` 缺失、不是数组或元素不是字符串时返回 `422`，不做隐式类型转换。

```
POST /flags/new_ui/batch  {"subjects": ["vip-001", "user-1", "blocked-007"]}
-> {"version": "ab12...", "flag": "new_ui", "results": [
     {"version": "ab12...", "flag": "new_ui", "enabled": true,  "reason": "force_on",   ...},
     {"version": "ab12...", "flag": "new_ui", "enabled": false, "reason": "rollout_out", ...},
     {"version": "ab12...", "flag": "new_ui", "enabled": false, "reason": "force_off",  ...}
   ]}
```

## 兼容性

- **老配置**：不含 `force_on / force_off / kill_switch` 的配置文件与环境变量层无需改动；三个字段缺省为空名单 / `false`，灰度与纯布尔开关行为与旧版完全一致（既有用例全部保留通过）。只写 `enabled` 或只写 `percentage` 的老配置升级后判定结果不变。
- **老接口**：`GET /flags/{name}`、`/config*` 的路径、方法与既有字段不变；单主体判定的旧 `reason`（`boolean_on/off`、`rollout_in/out`、`unknown_flag`）含义不变，本次仅**新增** `force_on / force_off / kill_switch` 三种 reason。`GET /config` 的开关视图额外带出三个新字段。
- **环境变量层**：暂不支持通过环境变量设置名单（名单为数组形态），名单通过配置文件配置；`APP_FLAG_* / APP_ROLLOUT_*` 语义不变。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 既有接口，行为不变 |
| GET | `/config` | 当前生效配置（密钥脱敏） |
| GET | `/config/status` | 当前/上一版本号与最近一次拒绝原因 |
| POST | `/config/reload` | 热更新；失败 422 + 可区分原因 |
| POST | `/config/rollback` | 回滚到上一可用版本 |
| GET | `/flags/{name}?subject=` | 开关判定（含版本与判定依据） |
| POST | `/flags/{name}/batch` | 批量判定，整批同版本（body：`{"subjects": [...]}`） |

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

# 验证强制名单、整体关闭与批量判定
cat > config.json <<'JSON'
{
  "flags": {
    "new_ui": {
      "enabled": true, "percentage": 30,
      "force_on": ["vip-001"],
      "force_off": ["blocked-007", "vip-001"],
      "kill_switch": false
    }
  }
}
JSON
curl -X POST localhost:8000/config/reload
curl 'localhost:8000/flags/new_ui?subject=vip-001'      # force_off（同时在两名单，关闭优先）
curl 'localhost:8000/flags/new_ui?subject=blocked-007'  # force_off
curl 'localhost:8000/flags/new_ui?subject=user-1'       # rollout_in / rollout_out
curl -X POST localhost:8000/flags/new_ui/batch \
  -H 'Content-Type: application/json' \
  -d '{"subjects": ["vip-001", "user-1", "blocked-007"]}'   # 三条 reason，同一个 version

# 一键全量关：改 kill_switch=true 后 reload，所有主体都是 kill_switch
sed -i 's/"kill_switch": false/"kill_switch": true/' config.json
curl -X POST localhost:8000/config/reload
curl -X POST localhost:8000/flags/new_ui/batch -H 'Content-Type: application/json' \
  -d '{"subjects": ["vip-001", "user-1"]}'                  # 全部 enabled=false, reason=kill_switch

# 坏名单整次拒绝：把名单写成字符串，reload 返回 422，服务仍是上一个好版本
echo '{"flags": {"new_ui": {"enabled": true, "percentage": 30, "force_on": "oops"}}}' > config.json
curl -X POST localhost:8000/config/reload                   # 422 type_mismatch

# 纯布尔开关（没设比例）同样受整体关闭与名单约束
cat > config.json <<'JSON'
{
  "flags": {
    "beta": {"enabled": true, "force_off": ["blocked-007"], "kill_switch": false}
  }
}
JSON
curl -X POST localhost:8000/config/reload
curl 'localhost:8000/flags/beta?subject=user-1'       # boolean_on
curl 'localhost:8000/flags/beta?subject=blocked-007'  # force_off（名单对纯布尔开关生效）
sed -i 's/"kill_switch": false/"kill_switch": true/' config.json
curl -X POST localhost:8000/config/reload
curl 'localhost:8000/flags/beta?subject=user-1'       # kill_switch（不是 boolean_off）
sed -i 's/"kill_switch": true/"kill_switch": false/' config.json
curl -X POST localhost:8000/config/reload
curl 'localhost:8000/flags/beta?subject=user-1'       # 恢复 boolean_on
```

测试覆盖：热更新失败保留旧版本、各类非法配置原因可区分、并发读写原子性、灰度边界（0/100/桶位边界）、分组稳定性与比例平滑扩张、密钥脱敏、**整体关闭与比例/名单叠加、两名单冲突以关闭为准、调比例不踢出名单外主体、坏名单整次拒绝、批量判定整批同版本、批量进行中热更新不串版本、空批次/重复主体/几千主体规模与 HTTP 入参校验**，以及**纯布尔开关（未设比例）叠加整体关闭与强制名单、整体关闭解除后恢复布尔状态、kill_switch 与 boolean_off 的 reason 可区分、纯布尔开关上坏字段整次拒绝、布尔/灰度开关混合互不干扰**。`uv run pytest -s` 可看到每条用例打印的版本号与判定依据。
