# 字幕时间轴三方合并 API

供字幕团队合并时间轴修订的 HTTP 服务。接收同一条时间轴的三个版本——
`base`（共同祖先）、`current`（我方修订）、`incoming`（对方修订）——
按 cue id 执行三方合并，检测字段级冲突、删除/修改冲突与时间轴重叠。

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
│   ├── main.py       # FastAPI 路由（/health、/merge、/）
│   ├── merge.py      # 三方合并与重叠检测纯逻辑
│   └── models.py     # Pydantic 请求/响应模型
├── tests/
│   └── test_api.py   # 18 个端到端用例（unittest + TestClient）
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
