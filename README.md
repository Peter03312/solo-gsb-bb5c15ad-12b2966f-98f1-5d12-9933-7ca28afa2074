# 字幕时间轴三方合并 API

供字幕团队合并时间轴修订的 HTTP 服务。接收同一条时间轴的三个版本——
`base`（共同祖先）、`current`（我方修订）、`incoming`（对方修订）——
按 cue id 执行三方合并，检测字段级冲突、删除/修改冲突与时间轴重叠。

- `POST /merge`：自动三方合并；存在冲突或重叠时只报告，不输出结果。
- `POST /resolve`：复用同一套三方合并识别冲突，由调用方携带人工决策
  （`field_choices` / `cue_choices`）解决全部冲突，再统一校验并返回 resolved 时间轴。

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

在 `/merge` 的三方合并之上，通过人工决策解决全部冲突。请求体沿用 `base` /
`current` / `incoming`，并增加两组决策：

| 字段            | 类型           | 说明                                                                              |
| --------------- | -------------- | --------------------------------------------------------------------------------- |
| `base`          | Cue[]          | 共同祖先版本                                                                      |
| `current`       | Cue[]          | 当前方修订                                                                        |
| `incoming`      | Cue[]          | 外部方修订                                                                        |
| `field_choices` | FieldChoice[]  | 字段级冲突决策，按 `cue_id` + `field` 定位                                        |
| `cue_choices`   | CueChoice[]    | cue 级冲突决策（delete_modify / add_add / invalid_interval），按 `cue_id` 定位 |

```json
{
  "field_choices": [
    {"cue_id": "cue-01", "field": "text", "choice": "current"}
  ],
  "cue_choices": [
    {"cue_id": "cue-02", "choice": "incoming"}
  ]
}
```

- `FieldChoice.choice`：`base` / `current` / `incoming`，该字段采纳对应侧取值；
  未发生冲突的字段仍按自动合并规则处理，无需也不能在 `field_choices` 中给出。
- `CueChoice.choice`：`current` / `incoming` / `delete`——整条采纳对应侧版本
  （某侧本就是删除时等同于删除该 cue），或直接删除。

#### 决策校验（422）

决策必须**恰好覆盖**三方合并识别出的全部冲突，以下情况返回 `422`，
`detail` 为逐项错误列表（含 `type` / `cue_id` / `field` / `message`）：

| `type`          | 含义                                                           |
| --------------- | -------------------------------------------------------------- |
| `duplicate`     | 同一冲突点（`cue_id`+`field`，或 cue 级 `cue_id`）被重复决策 |
| `unknown`       | 决策指向不存在冲突的 `cue_id`（或 `cue_id`+`field`）         |
| `type_mismatch` | 字段冲突放进了 `cue_choices`，或 cue 级冲突放进了 `field_choices` |
| `missing`       | 存在冲突却未给出对应决策                                       |

**无冲突时只允许空决策**（`field_choices` 与 `cue_choices` 均为空或缺省），
接口直接返回自动合并结果；任何非空决策都会按 `unknown` 拒绝。

#### 统一时间轴校验（409）

全部决策先通过上述校验，然后一次性应用，并对最终时间轴统一执行 Cue 校验：

- 决策组合出 `end <= start` 的非法区间；
- 存在区间重叠（严格相交，重叠时长 > 0；仅首尾相接不算重叠）。

任一情况返回 `409 status=invalid_timeline`，列出 `invalid_intervals`（cue id）
与 `overlaps`，**不输出任何部分结果**：

```json
{
  "status": "invalid_timeline",
  "invalid_intervals": ["a"],
  "overlaps": [
    { "cue_ids": ["a", "b"], "start": 4.0, "end": 4.5, "overlap": 0.5 }
  ]
}
```

#### 成功响应（200）

校验通过后，resolved 时间轴按 `start` 升序排序（平局依次按 `end`、`id`）：

```json
{
  "status": "resolved",
  "cues": [ { "id": "a", "start": 1.0, "end": 3.0, "text": "hello!", "speaker": null } ],
  "count": 1
}
```

#### 快速试用 /resolve

```bash
curl -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d '{
    "base":    [{"id":"a","start":0,"end":10,"text":"hi"}],
    "current": [{"id":"a","start":1,"end":3,"text":"hello"}],
    "incoming":[{"id":"a","start":7,"end":9,"text":"hola"}],
    "field_choices": [
      {"cue_id":"a","field":"start","choice":"current"},
      {"cue_id":"a","field":"end","choice":"current"},
      {"cue_id":"a","field":"text","choice":"incoming"}
    ],
    "cue_choices": []
  }'
# → {"status":"resolved","cues":[{"id":"a","start":1.0,"end":3.0,"text":"hola",...}],"count":1}
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
│   └── test_api.py   # 端到端用例（unittest + TestClient，覆盖 /merge 与 /resolve）
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
