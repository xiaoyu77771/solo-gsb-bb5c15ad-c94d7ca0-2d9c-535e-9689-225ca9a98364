# 本地依赖解析 API（Python · FastAPI · packaging）

一个完全本地化的软件依赖解析服务：仓库快照随请求提交，基于
[`packaging`](https://packaging.pypa.io/) 实现 PEP 440（版本/约束）、
PEP 503（包名规范化）与 PEP 508（environment marker），通过带回溯的搜索
生成**确定性锁定清单**；无解时返回从根需求到冲突包的**约束来源链**。

- **无远程访问**：不访问 PyPI 或任何索引，不做磁盘缓存，全部数据在请求内存中处理。
- **环境 marker（PEP 508）**：每条依赖可携带 marker；是否激活只由请求必填的
  `target_environment`（`python_version` / `sys_platform` / `platform_machine`）决定，
  **不读取宿主机**。只有激活的依赖进入全局约束、回溯、冲突来源链与 edges。
- **全局唯一版本**：同一包（PEP 503 规范化后）在整棵依赖树中只能选定一个版本。
- **高版本优先**：候选版本严格按 PEP 440 从高到低尝试，失败即回溯到更低版本。
- **环安全**：依赖环（含自环、菱形依赖）不会导致重复展开；环上的新约束仍会校验。
- **确定性**：结果只取决于快照内容与目标环境，与包键顺序、候选列表顺序、宿主机无关。

---

## 一、单命令启动（Docker Compose）

```bash
docker compose up --build
```

服务启动后监听：

| 项目 | 地址 |
| --- | --- |
| API 根地址 | `http://localhost:8000` |
| 解析接口 | `POST http://localhost:8000/resolve` |
| 健康检查 | `GET  http://localhost:8000/healthz` |
| 交互文档（Swagger） | `http://localhost:8000/docs` |
| OpenAPI Schema | `http://localhost:8000/openapi.json` |

端口在 `docker-compose.yml` 的 `ports: ["8000:8000"]` 中调整。

### 不使用 Docker 的本地运行方式

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 二、请求

### `POST /resolve`

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `repository` | `object` | 是 | 仓库快照：`{包名: [候选版本...]}`；包名按 PEP 503 规范化匹配（大小写/`_`/`.`/`-` 等价） |
| `repository.<pkg>[].version` | `string` | 是 | PEP 440 版本号，如 `1.2.3`、`2.0rc1` |
| `repository.<pkg>[].dependencies` | `array` | 否 | 该版本声明的依赖：`{"name", "specifier", "marker"}` |
| `repository.<pkg>[].dependencies[].specifier` | `string` | 否 | PEP 440 版本约束，如 `>=1.0,<2.0`；空串表示不约束 |
| `repository.<pkg>[].dependencies[].marker` | `string` | 否 | PEP 508 marker，如 `sys_platform == 'linux'`；空串表示无条件依赖。**仅可引用** `python_version`、`sys_platform`、`platform_machine` 三个变量 |
| `target_environment` | `object` | 是 | marker 的求值环境；值**完全取自请求**，服务端不读取宿主机 |
| `target_environment.python_version` | `string` | 是 | 目标 Python 版本，PEP 440 且按 PEP 508 约定传 `major.minor`，如 `3.11`（也可传 `3.12.3` 这类完整版本，按字面参与比较） |
| `target_environment.sys_platform` | `string` | 是 | 目标系统平台，如 `linux` / `darwin` / `win32` |
| `target_environment.platform_machine` | `string` | 是 | 目标机器架构，如 `x86_64` / `arm64` / `AMD64` |
| `root` | `string` | 是 | 根包名，必须存在于 `repository` |
| `specifier` | `string` | 否 | 对根包的 PEP 440 约束，如 `>=1.0,<2.0`；空串表示取最高版本 |
| `allow_prereleases` | `boolean` | 否 | 是否允许预发布（`a/b/rc/dev`），默认 `false`（pip 跟随语义：约束显式钉住预发布时仍可用） |

### Environment marker 语义

- 依赖形如 `{"name": "win-fs", "specifier": ">=1.0", "marker": "sys_platform == 'win32'"}`，
  支持完整 PEP 508 布尔表达式：比较运算（`== != < <= > >=`、`in` / `not in`）、
  `and` / `or` 与括号，例如
  `python_version >= '3.12' and (sys_platform == 'darwin' or platform_machine == 'arm64')`。
- marker 为假时，该依赖**在进入求解器之前即被剔除**：不会成为全局约束、不会触发回溯、
  不会出现在冲突来源链或 `edges` 中；即便它声明了根本无法满足的约束，也不影响结果。
- marker 语法非法、或引用了三个允许变量之外的任何名字（如 `os_name`、
  `python_full_version`、`platform_system`、`extra` 等），一律返回 `422`，
  错误信息定位到**包、版本与依赖**，例如：
  `app==1.0.0 的第 3 条依赖 'picky' 的 marker 非法：marker "python_full_version >= '3.11.0'" 引用了不允许的变量 ['python_full_version']`。
- 求值环境只包含请求给出的三个键：因此同一份请求在任何宿主机上结果完全一致。

### 成功请求示例（含回溯）

```bash
curl -s -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/resolve.json
```

`web-framework 3.1.0` 要求 `shared-util==2.0.0`，而 `auth-lib` 要求
`<2.0`，求解器回溯改选 `web-framework 2.4.0`：

```json
{
  "status": "ok",
  "root": "app",
  "root_version": "1.0.0",
  "locked": {
    "app": "1.0.0",
    "auth-lib": "0.9.0",
    "shared-util": "1.5.0",
    "web-framework": "2.4.0"
  },
  "edges": [
    {"from": "root", "to": "app", "specifier": "*"},
    {"from": "app==1.0.0", "to": "auth-lib", "specifier": "*"},
    {"from": "app==1.0.0", "to": "web-framework", "specifier": ">=2.0"},
    {"from": "auth-lib==0.9.0", "to": "shared-util", "specifier": "<2.0"},
    {"from": "web-framework==2.4.0", "to": "shared-util", "specifier": ">=1.0,<2.0"}
  ]
}
```

- `locked`：按规范化包名排序的**锁定清单**（包 → 全局唯一版本）。
- `edges`：按声明方/被依赖方排序的**依赖边**；空约束统一显示为 `*`，
  第一条恒为 `root → 根包`。

### 无解请求示例（返回约束来源链）

```bash
curl -s -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/conflict.json
```

`left` 与 `right` 对 `core` 给出互斥钉死约束，返回 `409`：

```json
{
  "status": "conflict",
  "message": "依赖约束无解，冲突包：core",
  "conflicts": [
    {
      "subject": "core",
      "kind": "incompatible",
      "message": "已选 core==1.0 与约束交集 ==1.0,==2.0 不兼容",
      "constraints": [
        {"chain": [
          {"package": null, "version": null, "requires": "*"},
          {"package": "app", "version": "1.0.0", "requires": "*"},
          {"package": "left", "version": "1.0", "requires": "==1.0"}
        ]},
        {"chain": [
          {"package": null, "version": null, "requires": "*"},
          {"package": "app", "version": "1.0.0", "requires": "*"},
          {"package": "right", "version": "1.0", "requires": "==2.0"}
        ]}
      ],
      "partial_selection": {"app": "1.0.0", "core": "1.0", "left": "1.0", "right": "1.0"}
    }
  ]
}
```

每条 `chain` 都是一跳约束来源：`package=null` 表示请求本身（根需求），
其后每一跳表示“`<package>==<version>` 声明了 `requires`”。同一冲突包上
所有互斥约束的来源链都会列出（回溯中尝试过的候选版本同样会出现）。

`kind` 取值：

- `no_candidates`：约束交集下没有任何候选版本（或该包不在快照中）；
- `incompatible`：包已选定版本后，环/菱形路径带来了不兼容约束。

### 跨环境示例（同一快照，不同 `target_environment`）

快照 `examples/cross-env-linux.json` / `examples/cross-env-macos.json` 使用完全
相同的 `repository`，只有目标环境不同。`app 3.0.0` 声明了四条带 marker 的依赖：

| 依赖 | 激活条件 |
| --- | --- |
| `std-fs >=1.0` | `sys_platform != 'win32'` |
| `win-fs >=1.0` | `sys_platform == 'win32'` |
| `accelerated-json >=2.0` | `python_version >= '3.12'` |
| `arm-crypto` | `platform_machine == 'arm64'` |

Linux / x86_64 / Python 3.11 —— 只锁定 `std-fs`：

```bash
curl -s -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/cross-env-linux.json
```

```json
{
  "status": "ok",
  "root": "app",
  "root_version": "3.0.0",
  "locked": {"app": "3.0.0", "std-fs": "1.4.0"},
  "edges": [
    {"from": "root", "to": "app", "specifier": "*"},
    {"from": "app==3.0.0", "to": "std-fs", "specifier": ">=1.0"}
  ]
}
```

macOS / arm64 / Python 3.12 —— 同一快照锁定另外三个依赖：

```bash
curl -s -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/cross-env-macos.json
```

```json
{
  "status": "ok",
  "root": "app",
  "root_version": "3.0.0",
  "locked": {
    "accelerated-json": "2.2.0",
    "app": "3.0.0",
    "arm-crypto": "0.9.0",
    "std-fs": "1.4.0"
  },
  "edges": [
    {"from": "root", "to": "app", "specifier": "*"},
    {"from": "app==3.0.0", "to": "accelerated-json", "specifier": ">=2.0"},
    {"from": "app==3.0.0", "to": "arm-crypto", "specifier": "*"},
    {"from": "app==3.0.0", "to": "std-fs", "specifier": ">=1.0"}
  ]
}
```

Windows / AMD64 时则只有 `win-fs` 激活（把示例里的 `target_environment` 改为
`{"python_version": "3.11", "sys_platform": "win32", "platform_machine": "AMD64"}`
即可验证）：`locked` 为 `{"app": "3.0.0", "win-fs": "1.1.0"}`。

### 其他状态码

| 状态码 | 含义 |
| --- | --- |
| `200` | 解析成功 |
| `409` | 约束无解（`status: "conflict"`）；或搜索超过内置尝试上限（`status: "limit"`） |
| `422` | 请求/快照非法：非 PEP 440 版本、非法约束串、PEP 508 marker 语法非法或引用越界变量（定位到包/版本/依赖）、`target_environment` 缺失或字段非法、根包不在快照中、字段校验失败等 |

`GET /healthz` 返回 `{"status":"ok"}`。

---

## 三、解析规则说明

1. **包名规范化**：统一使用 PEP 503（`Some_Pkg`、`some.pkg`、`some-pkg` 视为同一个包）。
2. **环境激活（PEP 508 marker）**：构造快照时先用请求的 `target_environment`
   对每条依赖的 marker 求值；仅激活的依赖进入后续求解，未激活项对锁定结果、
   edges 与冲突链完全不可见。求值环境只有 `python_version` / `sys_platform` /
   `platform_machine` 三个请求键，不读取宿主机。
3. **约束交集**：对每个包收集所有激活路径上的 PEP 440 约束，必须同时满足。
4. **选择次序**：在所有未决包中优先展开“可满足候选最少”的包（MRV），
   名称次序兜底；具体候选严格从高版本到低版本尝试。
5. **回溯**：候选版本展开后若与后续激活依赖冲突，放弃该版本，尝试下一个更低版本；
   每次尝试在独立的状态克隆上进行，失败不会污染其他分支。
6. **环处理**：已选定的包被再次要求时只校验约束、不重复展开，因此环不会无限递归；
   若环上带来矛盾约束，按 `incompatible` 报出。
7. **预发布**：默认排除 `a/b/rc/dev`；`allow_prereleases=true` 放开；
   或约束显式钉住预发布（如 `==2.0rc1`）时也会被接受。
8. **确定性**：候选按 PEP 440 降序、输出按键排序；相同请求（含目标环境）
   重复解析结果逐字节一致，与运行服务的宿主机无关。

### 安全护栏

- **尝试上限**（默认 100,000 次候选展开）：防止恶意构造的快照造成指数级搜索，
  超出返回 `409 / status:"limit"`。
- **依赖链深度上限**（默认 5,000 层）：超出同样以 `limit` 报出，而不是进程栈溢出。

## 四、项目结构

```
app/
  main.py        # FastAPI 路由与错误映射
  resolver.py    # 回溯求解器（候选排序 / MRV / 来源链重建）
  repository.py  # 快照规范化、PEP 440/503 解析、marker 激活过滤
  markers.py     # PEP 508 marker 编译/越权校验/纯请求环境求值
  schemas.py     # 请求/响应模型（含 target_environment）
examples/        # curl 示例请求体（含跨环境对：cross-env-*.json）
tests/           # 求解器 / marker / HTTP 层测试（pytest + FastAPI TestClient）
Dockerfile
docker-compose.yml
requirements.txt
```

## 五、运行测试

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest -q
```
