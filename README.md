# 方志馆藏公告抓存索引 (Gazetteer Web-Archive Index)

地方志编纂员用于回答「馆藏公告的正文在哪两次抓存之间改过」的查询服务。
FastAPI 提供查询接口，SQLite 保存抓存索引与摘要校验结果，原始 WARC 卷留档于磁盘。

## 运行

```bash
docker compose up --build        # 服务监听 :8000
# 打开 http://127.0.0.1:8000/docs  (Swagger UI)
```

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
DATA_DIR=./data uvicorn app.main:app --port 8000
python -m pytest tests/ -q       # 自测
```

## 演示（换卷前后查区间）

```bash
python scripts/make_demo_warcs.py demo   # 生成演示 WARC 卷
scripts/demo_flow.sh                     # 需服务已在 :8000 运行
```

流程：上传 vol1（t1=V1, t2=V2 两个 response）与 vol2（t3=revisit→V2,
t4=V3, t5=摘要损坏的 response, t6=revisit→V1）→ 查 `/changes` 得换卷前区间
(t1→t2, t3→t4, t4→t6，缺证 t5) → 用 vol1b（只含 t1=V1）整卷替换 vol1 →
再查得 t1→t4, t4→t6，缺证变为 t3+t5（vol2 的 t3 重访因最后一份 V2 正文被移除
而失去证据）→ 最后演示跨卷同刻冲突返回 409 且旧卷保留。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `PUT` | `/volumes/{volume}/warc` | 按卷号上传**未压缩** WARC（multipart 文件）；同号卷整卷替换，失败保留旧卷 |
| `GET` | `/volumes` | 列出馆藏卷（文件名、SHA-256、记录数、入库时间） |
| `DELETE` | `/volumes/{volume}` | 移除整卷 |
| `GET` | `/changes?url=…` | 对指定 URL 按时刻比较相邻两次**有证据**的抓存，正文不同即返回这两个时刻；另列缺证抓存 |

`/changes` 响应：

```json
{
  "url": "…",
  "changes": [{"from_ts": "…", "to_ts": "…", "from_digest": "…", "to_digest": "…"}],
  "unevidenced": ["2025-03-01T09:00:00Z"],
  "captures": [{"ts": "…", "type": "response|revisit", "digest": "…",
                "evidenced": true, "volume": "vol1", "http_status": 200}]
}
```

## 处理规则

- **记录范围**：只处理带 `WARC-Payload-Digest: sha1:…` 的 `response` 与
  `revisit` 记录；其余类型（warcinfo/request/metadata 等）与其他摘要算法跳过并计数。
- **HTTP 正文限无编码**：`Content-Encoding`（非 identity）或非 chunked 的
  `Transfer-Encoding` 一律跳过；`Transfer-Encoding: chunked` 先还原分块再校验。
- **摘要校验**：response 的正文 SHA-1（base32）与卷头摘要一致才可作证
  （`valid=1` 存入 SQLite）；不一致仍为抓存但缺证。
- **重访证据**：revisit 只有在**现存卷**中存在通过校验的同摘要 response
  正文时才可作证。该判定在查询时对联（部分索引
  `idx_verified_response_digest`），因此移除最后一份匹配正文后，其他卷的
  重访自动失去证据，且换卷无需重扫他卷。
- **时刻唯一**：`(url, ts)` 为全库主键（UTC 规范化后的 ISO-8601）。同号卷
  替换时与本卷旧记录不冲突；与他卷冲突或文件内重复 → `409`，整卷不入库。
- **整卷替换**：新卷先完整解析校验，再在单个事务里「删旧卷行 + 冲突检查 +
  插新行 + 更新卷目」，任一步失败即回滚，旧卷（索引与留档文件）原样保留；
  原卷文件先写临时文件 fsync，事务提交后才 `os.replace` 落位。
- **换卷代价**：删除按卷索引、插入与冲突检查只触及新旧两卷的记录数；
  重访证据不在入库时物化，索引在换卷后立即可查。
- **查询一致性**：每次查询在单个 SQLite 读事务内完成，只看到一个已提交状态
  （WAL 模式，写入不阻塞读取）。

## 存储

```
$DATA_DIR/
├── index.sqlite3        # volumes 表 + captures 表（含 response 校验结果 valid）
└── volumes/{卷号}.warc   # 原卷留档（上传字节原样保存）
```

## 已知限制

- 上传整体读入内存解析，未做流式分块（超大卷需调内存/改为流式）。
- 单文件 SQLite，适合单实例；多实例部署需换外部数据库。
- URL 精确匹配，不做规范化（大小写、默认端口等不合并）。
- 未鉴权，默认部署在内网。
