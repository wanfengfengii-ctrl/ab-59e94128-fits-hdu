# FITS Audit Service

天文数据中心归档前的严格 FITS 文件审计服务。对上传的观测文件逐 HDU 核对
FITS 头卡、字节边界、数据补齐区以及标准 `DATASUM` / `CHECKSUM` 校验字，任何
截断、拼接、字段冲突或校验不符都拒绝整份文件，并稳定报告**最早失败的 HDU
编号（零基）、原因码与可定位的字节偏移**。

纯 Python 标准库实现（无第三方运行时依赖），以线程化 `http.server` 提供
HTTP 服务；校验和算法已与 Astropy 8 双向逐字节交叉验证。

## 接口

### `POST /api/fits/audit`

* **Content-Type**：必须为 `application/fits`
* **Body**：完整 FITS 文件，上限 16 MiB
* **成功**：HTTP `200`，`"status": "accepted"`
* **拒绝**：HTTP `422`，`"status": "rejected"`，并给出
  `error.hdu`（零基 HDU 编号）、`error.reason`（稳定原因码）、
  `error.offset`（文件内绝对字节偏移）

成功响应示例：

```json
{
  "status": "accepted",
  "conclusion": "ACCEPT",
  "size_bytes": 11520,
  "hdus": [
    {
      "index": 0,
      "type": "PRIMARY",
      "byte_range": [0, 5760],
      "data_bytes": 100,
      "datasum": { "status": "valid", "offset": 320,
                   "stored": "12345", "actual": "12345" },
      "checksum": { "status": "valid", "offset": 400,
                    "stored": "oRHmrOGkoOGkoOGk",
                    "expected": "oRHmrOGkoOGkoOGk" }
    }
  ],
  "error": null
}
```

`byte_range` 为**半开**字节区间 `[start, end)`，`data_bytes` 是按
`BITPIX/NAXIS/NAXISn/PCOUNT/GCOUNT` 推导出的逻辑数据字节数（不含 2880
补齐）。拒绝时 `hdus` 中只包含在失败之前已成功审计的 HDU。

### `GET /health`

健康检查，返回 `200 {"status": "ok"}`，供 Docker / Compose 健康探针使用。

## 运行（Docker Compose）

```bash
cd app

# 启动常驻 API；宿主机端口可通过环境变量配置
FITS_API_PORT=9090 docker compose up -d web

# 健康检查
curl -s http://localhost:9090/health

# 审计一个文件
curl -s -X POST http://localhost:9090/api/fits/audit \
     -H 'Content-Type: application/fits' \
     --data-binary @observation.fits | python -m json.tool
```

默认宿主机端口为 `8080`（`${FITS_API_PORT:-8080}`）。

### 一次性 verify 服务

```bash
docker compose run --rm verify
```

`verify` 会等待 `web` 健康后依次执行：

1. 应用构建（字节码编译 + 模块导入）；
2. 全部单元测试；
3. **合法文件**冒烟：含主 HDU 与 IMAGE 扩展、校验字完整 → 期望
   `200 ACCEPTED`；
4. **摘要损坏**冒烟：翻转数据字节 → 期望 `422 REJECTED`，原因码为
   `DATASUM_MISMATCH` / `CHECKSUM_MISMATCH`，并定位到最早失败 HDU；
5. **截断文件**冒烟：数据块被截短 → 期望 `422 REJECTED`，原因为
   `TRUNCATED_*`。

它是**一次性服务**：打印逐项汇总后自行退出。退出码为位掩码，便于 CI 精确定位
失败环节：

| 位 | 值 | 关卡 |
|----|----|------|
| 0 | 1  | 应用构建（编译 + 导入） |
| 1 | 2  | 单元测试 |
| 2 | 4  | 合法文件 HTTP 裁决 |
| 3 | 8  | 摘要损坏 HTTP 裁决 |
| 4 | 16 | 截断文件 HTTP 裁决 |

全部通过时退出码为 `0`。

### 本地运行

```bash
# 仅跑测试
python -m unittest discover -s tests -v

# 一次性验证（自动拉起本地服务，跑完即退）
python scripts/verify.py --spawn-server

# 针对已在运行的服务
python scripts/verify.py --base-url http://127.0.0.1:8080
```

## 审计规则（拒绝条件）

每个 HDU 内严格按字节顺序检查，遇到第一个违例即中止并拒绝整份文件：

* 头卡非可打印 ASCII（`NON_ASCII_CARD`）、关键字非法、缺少 `= ` 值指示符
  （`INVALID_CARD`）；
* 必备卡缺失或乱序：`SIMPLE`/`XTENSION`、`BITPIX`、`NAXIS`、`NAXISn`、
  扩展 HDU 的 `PCOUNT`、`GCOUNT`（`MISSING_KEYWORD`、`UNEXPECTED_KEYWORD`）；
* 关键字重复（`DUPLICATE_KEYWORD`）；
* `SIMPLE` 非 `T`、`BITPIX` 不属于 `{8,16,32,64,-32,-64}`、轴长为负/非整数、
  IMAGE HDU 的 `PCOUNT != 0`、`GCOUNT != 1`
  （`INVALID_KEYWORD_VALUE`、`PCOUNT_CONFLICT`、`GCOUNT_CONFLICT`）；
* 扩展不是 `IMAGE`（`UNSUPPORTED_EXTENSION`）；扩展超过 15 个
  （`TOO_MANY_HDUS`）；存在扩展但主 HDU 没有 `EXTEND = T`
  （`MISSING_EXTEND`）；第二个主 HDU（拼接文件特征，`DUPLICATE_PRIMARY`）；
* 头未以 `END` 结束、未补齐至 2880 或补齐区出现非空格
  （`TRUNCATED_HEADER`、`INVALID_HEADER_PADDING`）；
* 数据块越过文件尾（`TRUNCATED_DATA`）、数据补齐区出现非零字节
  （`NONZERO_PADDING`）；
* 末 HDU 之后存在尾随字节（`TRAILING_BYTES`）；
* 缺少/畸形 `DATASUM`、`CHECKSUM`（`MISSING_DATASUM`、`MISSING_CHECKSUM`、
  `MALFORMED_*`）或重算不符（`DATASUM_MISMATCH`、`CHECKSUM_MISMATCH`）。

逻辑数据长度严格按
`|BITPIX|/8 × GCOUNT × (PCOUNT + ∏ NAXISn)` 计算，再向上补齐到 2880；补齐
区域逐字节核验必须全零。

## 校验和约定

`DATASUM` 是数据块（含 2880 零补齐）的 32 位反码和：大端 32 位字累加、
循环进位折叠，最后一个不足四字节的字以零右补齐。`CHECKSUM` 是头块（将
`CHECKSUM` 值替换为十六个 `'0'`）与数据块反码和的字符编码值，遵循 FITS
Checksum Convention 附录 A.7.2。实现与 Astropy 8 的 `_compute_checksum` /
`_char_encode` 完全一致（见开发期交叉验证脚本，40 个单元测试）。

## 目录结构

```
app/
├── Dockerfile
├── docker-compose.yml
├── README.md
├── fits_audit/
│   ├── __init__.py
│   ├── audit.py       # 严格 HDU 解析与裁决引擎
│   ├── checksum.py    # 反码和 + A.7.2 字符编码
│   └── server.py      # 标准库 HTTP 前端
├── scripts/
│   └── verify.py      # 一次性构建 + 测试 + HTTP 冒烟裁决
└── tests/
    ├── __init__.py
    ├── fixtures.py    # 纯标准库 FITS/校验和固件构造器
    └── test_audit.py  # 40 个单元测试
```
