# 字幕时间轴三方合并 API

供字幕团队合并时间轴修订的 HTTP 服务。接收同一条时间轴的三个版本——
`base`（共同祖先）、`current`（我方修订）、`incoming`（对方修订）——
按 cue id 执行三方合并，检测字段级冲突、删除/修改冲突与时间轴重叠。
`POST /resolve` 进一步支持对识别出的冲突提交人工决策，输出解决后的时间轴。

技术栈：Python 3.11 · FastAPI · Pydantic v2 · Uvicorn，全部依赖与启动初始化均在容器内完成。

## 一条命令启动

```bash
docker compose up --build -d
```

服务地址与端口：

- 服务地址：**http://localhost:8000**
- 健康检查：http://localhost:8000/health
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- OpenAPI Schema：http://localhost:8000/openapi.json

停止服务：`docker compose down`；查看日志：`docker compose logs -f`。

> 不使用 Compose 时也可：
> `docker build -t subtitle-merge . && docker run --rm -p 8000:8000 subtitle-merge`

## 接口

### GET /health

健康检查，返回 `200 {"status":"ok"}`。

### POST /merge

请求体：

| 字段       | 类型    | 说明            |
| ---------- | ------- | --------------- |
| `base`     | Cue[]   | 共同祖先版本    |
| `current`  | Cue[]   | 当前方修订      |
| `incoming` | Cue[]   | 外部方修订      |

Cue 结构（时间单位为秒，支持小数）：

```json
{"id": "cue-01", "start": 12.5, "end": 15.0, "text": "你好", "speaker": null}
```

- `id`：非空字符串，合并键；同一版本内不允许重复。
- `start` / `end`：非负数字，且 `end > start`。
- `text`：字幕文本。
- `speaker`：可选说话人；缺省与 `null` 等价。

#### 合并规则（相对 base）

1. 只有一侧修改某字段 → 采纳该侧的值；
2. 两侧都未改 → 保留 base 的值；
3. 两侧改成相同值 → 采纳该值；
4. 两侧改成不同值 → 记录**字段级冲突**（含 base/current/incoming 三侧取值）；
5. 一侧删除、另一侧修改同一 cue → 记录 **delete_modify** cue 级冲突（删除与修改不可自动调和）；一侧删除而另一侧未改时，删除直接生效；两侧都删除则干净删除；
6. 两侧各自新增同一 id：内容一致则采纳，不一致记录 **add_add** 冲突并列出差异字段；
7. 无任何冲突时，结果按 `start` 升序排序（平局依次按 `end`、`id`）；
8. 排序后检查时间轴：只要存在区间重叠（严格相交，重叠时长 > 0；仅首尾相接 `end == start` 不算重叠），返回**所有**重叠 cue id 对及重叠区间，**不输出合并版本**；
9. 合并冲突优先于重叠检查：存在冲突时只报告冲突。

#### 响应

- `200` 合并成功：

```json
{
  "status": "merged",
  "cues": [ { "id": "a", "start": 0.0, "end": 2.0, "text": "hello", "speaker": null } ],
  "count": 1
}
```

- `409 status=conflict`：cue 级冲突（删除/修改、同 id 新增不一致、合并后非法区间）与字段级冲突：

```json
{
  "status": "conflict",
  "cue_conflicts": [
    {
      "cue_id": "a",
      "type": "delete_modify",
      "side": "current",
      "fields": ["text"],
      "current": null,
      "incoming": { "id": "a", "start": 0.0, "end": 2.0, "text": "hello!", "speaker": null }
    }
  ],
  "field_conflicts": [
    { "cue_id": "b", "field": "text", "base": "hi", "current": "one", "incoming": "two" }
  ]
}
```

- `409 status=overlap`：无冲突但时间轴重叠，不返回合并结果：

```json
{
  "status": "overlap",
  "overlaps": [
    { "cue_ids": ["a", "b"], "start": 4.0, "end": 5.0, "overlap": 1.0 }
  ]
}
```

- `422`：请求体校验失败（重复 id、`end <= start` 等）。

### POST /resolve

在与 `/merge` 完全相同的三方合并之上，携带人工冲突决策并返回解决后的时间轴。
冲突识别复用同一套三方合并逻辑；只有被 `/merge` 报告为冲突的位置才接受决策。

请求体在 `base` / `current` / `incoming` 之外增加两个字段（均可省略，默认为空列表）：

| 字段            | 类型           | 说明                                                                                |
| --------------- | -------------- | ----------------------------------------------------------------------------------- |
| `field_choices` | FieldChoice[]  | 字段级冲突决策，按 `cue_id` + `field` 定位                                          |
| `cue_choices`   | CueChoice[]    | cue 级冲突决策（`delete_modify` / `add_add` / `invalid_interval`），按 `cue_id` 定位 |

```json
{
  "base":    [{"id": "a", "start": 0, "end": 2, "text": "hi"}],
  "current": [{"id": "a", "start": 0, "end": 2, "text": "one"}],
  "incoming":[{"id": "a", "start": 0, "end": 2, "text": "two"}],
  "field_choices": [{"cue_id": "a", "field": "text", "choice": "current"}],
  "cue_choices": []
}
```

- **FieldChoice**：`{"cue_id", "field", "choice"}`
  - `field`：`start` / `end` / `text` / `speaker`；
  - `choice`：`base` / `current` / `incoming` —— 该字段采纳哪一侧的值；
  - 同一条 cue 上未发生冲突的字段仍按三方合并规则自动取值。
- **CueChoice**：`{"cue_id", "choice"}`
  - `choice`：`current` / `incoming` —— 整条 cue 采用该侧版本；`delete` —— 不输出该 cue；
  - `delete_modify` 中选择了已删除的一侧时，效果等同 `delete`。

#### 决策校验（先于应用，任一不过即 `422`，不应用任何决策）

1. **重复（duplicate）**：同一 `(cue_id, field)` 出现多条字段决策，或同一 `cue_id` 出现多条 cue 决策；
2. **未知（unknown）**：决策针对的冲突不存在（该 cue/字段本来就没有冲突，或 cue id 不存在）；
3. **类型不匹配（type_mismatch）**：字段决策发给了 cue 级冲突，或 cue 决策发给了字段级冲突；
4. **遗漏（missing）**：三方合并识别出的每个冲突都必须给出对应决策，缺一不可；
5. 枚举值非法、请求体结构错误同样返回 `422`。
6. **无冲突时只允许空决策**：没有任何冲突时携带任何决策都会得到 `unknown`；空决策（或省略两个字段）则直接走后续校验并返回结果。

#### 应用后的统一校验

决策全部合法时才应用，并对最终时间轴统一校验：

- 某条 cue 形成非法区间（`end <= start`，例如两端点分别选用了不同侧）→ `409`，列出该 cue id；
- 时间轴存在严格重叠（首尾相接不算）→ `409`，列出所有重叠区间；
- 两种问题可能同时报告；任一存在都**不输出部分结果**；
- 全部合法时，结果按 `start` 升序（平局依次按 `end`、`id`）返回。

#### 响应

- `200` 解决成功：

```json
{
  "status": "resolved",
  "cues": [ { "id": "a", "start": 0.0, "end": 2.0, "text": "one", "speaker": null } ],
  "count": 1
}
```

- `422 status=decision_error`：决策无效（下例为对不存在冲突的字段下发了决策）：

```json
{
  "status": "decision_error",
  "errors": [
    { "cue_id": "a", "field": "start", "type": "unknown", "message": "cue a 的字段 start 不存在字段级冲突" }
  ]
}
```

  错误类型取值：`duplicate` / `unknown` / `type_mismatch` / `missing`；字段决策错误带 `field`，cue 决策错误的 `field` 为 `null`。

- `409 status=timeline_error`：应用决策后非法区间或重叠，不返回时间轴：

```json
{
  "status": "timeline_error",
  "invalid_intervals": ["a"],
  "overlaps": [
    { "cue_ids": ["a", "b"], "start": 3.0, "end": 4.0, "overlap": 1.0 }
  ]
}
```

#### 典型流程

先调用 `POST /merge` 取得 `field_conflicts` / `cue_conflicts`（或直接调 `/resolve`），
按返回的 `cue_id`（及 `field`）逐项给出决策后调用 `POST /resolve`；
决策导致的非法区间/重叠修正后重新提交即可。

```bash
curl -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d '{
    "base":    [{"id":"a","start":0,"end":10,"text":"keep"}],
    "current": [{"id":"a","start":8,"end":9,"text":"cur"}],
    "incoming":[{"id":"a","start":1,"end":3,"text":"inc"}],
    "field_choices": [
      {"cue_id":"a","field":"start","choice":"incoming"},
      {"cue_id":"a","field":"end","choice":"current"},
      {"cue_id":"a","field":"text","choice":"base"}
    ]
  }'
```

起点取 incoming（1）、终点取 current（9）、文本保留 base，得到合法区间：

```json
{"status":"resolved","cues":[{"id":"a","start":1.0,"end":9.0,"text":"keep","speaker":null}],"count":1}
```

## 快速试用

```bash
curl -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":    [{"id":"a","start":0,"end":2,"text":"hi"}],
    "current": [{"id":"a","start":0,"end":2,"text":"hello"}],
    "incoming":[{"id":"a","start":0.5,"end":2,"text":"hi"}]
  }'
```

两侧改动不同字段（current 改文本、incoming 改起点），自动合并为：

```json
{"status":"merged","cues":[{"id":"a","start":0.5,"end":2.0,"text":"hello","speaker":null}],"count":1}
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py       # FastAPI 路由（/health、/merge、/resolve、/）
│   ├── merge.py      # 三方合并、冲突决策应用与重叠检测纯逻辑
│   └── models.py     # Pydantic 请求/响应模型
├── tests/
│   └── test_api.py   # 46 个端到端用例（unittest + TestClient）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 本地开发（不使用容器）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```
