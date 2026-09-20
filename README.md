# GS1 条码元素串 / Digital Link 解析 API

基于 Python + FastAPI 的 GS1 解析服务。`/parse` 接口同时接受两种输入：

1. **元素串（Element String）**：扫码枪原始输出（含 ASCII 29 / FNC1 分隔符）；
2. **GS1 Digital Link URI**：以 `http://` 或 `https://` 开头的链接，AI/值对位于路径
   （`/AI/值/AI/值`）和查询参数（`?AI=值&AI=值`）中。

两种输入都返回保持原顺序的结构化元素，并完成同一套字符集、长度、GTIN Mod-10
校验位与真实日期校验。重量 AI（310n/320n）在两种输入下行为完全一致，响应除
原始 `value` 外还会返回 `decimal_value`（按隐含小数位解释后的数值）与 `unit`。

## 一键启动

```bash
docker compose up --build
```

需要后台运行时，一条命令即可（守护态启动、自动构建）：

```bash
docker compose up --build -d
```

启动后通过 **8080** 端口访问：

- API 地址：http://localhost:8080
- 交互式文档（Swagger UI）：http://localhost:8080/docs
- 健康检查：http://localhost:8080/health

查看日志与停止服务：

```bash
docker compose logs -f   # 查看日志
docker compose down      # 停止
```

## 支持的 AI

| AI   | 含义        | 长度            | 字符集                          |
| ---- | ----------- | --------------- | ------------------------------- |
| 01   | GTIN        | 固定 14 位数字  | 0-9，含 Mod-10 校验位验证       |
| 17   | 效期 YYMMDD | 固定 6 位数字   | 0-9，校验真实日期（含闰年）     |
| 10   | 批号        | 可变 1-20 字符  | GS1 AI 字符集 82                |
| 21   | 序列号      | 可变 1-20 字符  | GS1 AI 字符集 82                |
| 310n | 净重（千克 kg） | AI 4 位 + 固定 6 位数字 | 0-9；`n` 为 0-9 的隐含小数位数 |
| 320n | 净重（磅 lb）   | AI 4 位 + 固定 6 位数字 | 0-9；`n` 为 0-9 的隐含小数位数 |

重量 AI 说明：

- 标识符共 4 位：前缀 `310`（千克）或 `320`（磅）+ 1 位小数位指示符 `n`
  （0-9），其后**固定 6 位数字**，按长度自动定界（与 01/17 一样无需分隔符）。
- 解析器按**最长可匹配**识别四位标识符：`310n`/`320n` 优先于两位 AI；
  例如 `3103...` 识别为 AI `3103`，而 `3110...` 仍按两位 AI `31` 处理
  （`31` 不受支持，返回 `UNSUPPORTED_AI`）。
- 值按整数除以 10^n 解释：`3103 002513` → `decimal_value = 2.513`，
  `unit = "kg"`；`3202 002513` → `25.13 lb`。`n = 0` 时为整数；
  `n > 6` 时高位补隐含的零，如 `3109 000001` → `0.000000001 kg`。
- 响应中的 `value` 保留**原始 6 位数字**（不插小数点），仅重量元素额外携带
  `decimal_value`（JSON number）与 `unit`（`"kg"` / `"lb"`）两个字段。

解析规则：

- 固定长度字段（01、17、310n、320n）按长度自动定界，无需分隔符。
- 可变长度字段（10、21）除位于串末尾外，必须以分隔符 ASCII 29（GS，扫码枪 FNC1）
  结束；JSON 中以 `\u001d` 表示。
- AI 17 的日字段为 `00` 时按 GS1 规范表示当月最后一天；年份按 20YY 解释。
- `data` 以 `http://` 或 `https://` 开头时按 GS1 Digital Link URI 解析，否则按
  元素串解析。

### Digital Link URI 规则

- 路径段必须成对出现：`/AI/值/AI/值...`；查询参数按 `AI=值&AI=值` 解析（参数没有
  等号时值为空，会触发 `FIELD_EMPTY`）。
- 值先做百分号 UTF-8 解码（`%XX`），再执行与元素串完全相同的字符集、长度、校验位与
  日期校验；`offset` 始终指向**原始 URI** 中的字节位置（解码后字符报错时定位到它的
  首个编码字节，即 `%` 所在字节）。
- 未知 AI、路径内重复 AI、查询参数内重复 AI，以及同一 AI 同时出现在路径和查询参数
  中，均返回 `422`。
- 响应中路径提取到的元素按路径顺序在前，查询参数提取到的元素按参数顺序在后。
- 片段（`#...`）不属于 Digital Link 数据，解析时忽略。

## 使用示例

### 元素串

请求（注意批号与序列号之间的 `\u001d` 分隔符）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "01095011015300031725013110ABC123\u001d21SN0001"}'
```

响应 `200 OK`，元素按原始顺序返回，`offset` 为 AI 在输入中的字节偏移：

```json
{
  "elements": [
    { "ai": "01", "name": "GTIN",     "value": "09501101530003", "offset": 0  },
    { "ai": "17", "name": "EXPIRY",   "value": "250131",         "offset": 16 },
    { "ai": "10", "name": "BATCH/LOT","value": "ABC123",         "offset": 24 },
    { "ai": "21", "name": "SERIAL",   "value": "SN0001",         "offset": 33 }
  ]
}
```

### GS1 Digital Link URI

路径和查询参数混用的请求（路径中的元素在前，查询参数中的元素在后）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/01/09501101530003/17/250131?10=ABC123&21=SN0001"}'
```

响应与元素串完全一致，`offset` 是 AI 在**原 URI** 中的字节偏移：

```json
{
  "elements": [
    { "ai": "01", "name": "GTIN",      "value": "09501101530003", "offset": 19 },
    { "ai": "17", "name": "EXPIRY",    "value": "250131",         "offset": 37 },
    { "ai": "10", "name": "BATCH/LOT", "value": "ABC123",         "offset": 47 },
    { "ai": "21", "name": "SERIAL",    "value": "SN0001",         "offset": 57 }
  ]
}
```

值可使用百分号编码，按 UTF-8 解码后再做同一套校验。下面的批号解码后为
`Lot A/100`（百分号编码指向原 URI 字节位置，示例中 `/` 编码为 `%2F`；
注意空格不属于字符集 82，仅作编码演示）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/10/Lot%20A%2F100"}'
```

该请求会返回 `422 INVALID_CHARACTER`，`offset` 指向编码空格的 `%20` 首字节。
仅含合法字符的示例：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/10/Lot%2FA%2F100"}'
```

解码后批号为 `Lot/A/100`，返回 `200`。

路径段必须成对，例如 `.../01/09501101530003/17` 会返回 `422`
（`UNPAIRED_PATH`）；同一 AI 不能重复，也不能同时出现在路径和查询参数中
（`DUPLICATE_AI`）。

### 重量 AI（310n / 320n）

元素串与 Digital Link 两种输入返回完全相同的重量字段。元素串示例（`n=3`，
`002513` 表示 2.513 kg）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "01095011015300033103002513"}'
```

Digital Link 示例（路径中的千克重量，查询参数中的磅重量）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/01/09501101530003/3103/002513?3202=002513"}'
```

两种请求的重量元素结构一致，`value` 为原始 6 位数字，`decimal_value` 为按
`n` 位隐含小数点解释后的数值，`unit` 为单位：

```json
{
  "ai": "3103",
  "name": "NET WEIGHT (KG)",
  "value": "002513",
  "decimal_value": 2.513,
  "unit": "kg",
  "offset": 16
}
```

```json
{
  "ai": "3202",
  "name": "NET WEIGHT (LB)",
  "value": "002513",
  "decimal_value": 25.13,
  "unit": "lb",
  "offset": 49
}
```

非重量元素（01/10/17/21）不会出现 `decimal_value` 与 `unit` 字段。

## 批量重量汇总 `POST /weight-summary`

一次提交多条扫码字符串，按 **GTIN（AI 01）+ 批号（AI 10）** 分组汇总净重。

- 请求体：`{"scans": ["...", ...], "target_unit": "kg"}`。`target_unit`
  可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`scans` 中的每一项既可以是
  元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫描必须包含**恰好一个 AI 01**、**零个或一个 AI 10**（批号可缺省）和
  **恰好一个重量 AI**（310n 或 320n，允许混合千克/磅）；可有可无的 AI 17/21
  会被忽略。缺少/重复 AI 01、重复 AI 10、缺少或出现多个重量 AI，或任一条
  扫描解析失败，整个请求返回 `422`，`detail.scan_index` 给出出错下标
  （从 0 开始），`detail.error` 为该条扫描的原始错误（结构同 `/parse`）。
- 重量一律使用 `Decimal` 从**原始六位数字与精度位 n** 重建（`整数 / 10^n`），
  不使用解析结果中的 float `decimal_value`，累加也全程 `Decimal`，禁止
  float 累加。换算系数固定为 **1 lb = 0.45359237 kg**（磅→千克相乘，
  千克→磅按该系数的精确倒数相除）。
- 分组按首次出现顺序返回；无批号（缺省 AI 10）的扫描归入 `batch: null`
  的独立分组。每组包含 `gtin`、`batch`、`count`、`unit` 与 `total`：
  `total` 是组内所有重量转换到 `target_unit` 后之和，最终按**六位小数
  half-even（银行家舍入）**输出为 JSON 数字（如 `2.513000`，恒为六位，
  即使为零也显示 `0.000000`）；舍入在分组累加完成后一次性进行。
- `scans` 为空数组时返回 `200` 与空的 `groups`。

请求示例（同一 GTIN+批号下一条千克、一条磅，目标单位 kg）：

```bash
curl -X POST http://localhost:8080/weight-summary \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "scans": ["010950110153000310B1\u001d3103002513", "010950110153000310B1\u001d3202002513", "010950110153000310B2\u001d3106123456"]}'
```

`2.513 kg + 25.13 lb × 0.45359237 = 2.513 + 11.3987762581 = 13.9117762581`，
六位 half-even 舍入为 `13.911776`；响应 `200 OK`：

```json
{
  "target_unit": "kg",
  "groups": [
    {"gtin": "09501101530003", "batch": "B1", "count": 2, "unit": "kg", "total": 13.911776},
    {"gtin": "09501101530003", "batch": "B2", "count": 1, "unit": "kg", "total": 0.123456}
  ]
}
```

目标单位为磅时（`1 kg = 1 / 0.45359237 ≈ 2.20462262 lb`）：

```bash
curl -X POST http://localhost:8080/weight-summary \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "lb", "scans": ["01095011015300033100000001"]}'
```

```json
{"target_unit": "lb", "groups": [{"gtin": "09501101530003", "batch": null, "count": 1, "unit": "lb", "total": 2.204623}]}
```

出错时（下例下标 1 的 GTIN 校验位错误）返回 `422`：

```json
{
  "detail": {
    "scan_index": 1,
    "error": {
      "error": "INVALID_CHECK_DIGIT",
      "message": "GTIN check digit mismatch: expected 3, found 4",
      "offset": 15,
      "expected": "Mod-10 check digit 3",
      "found": "4"
    }
  }
}
```

结构类错误（缺少/重复 AI）使用同样的 `detail` 结构，错误码为 `MISSING_AI`
或 `DUPLICATE_AI`，例如重复的重量 AI：

```json
{
  "detail": {
    "scan_index": 0,
    "error": {
      "error": "DUPLICATE_AI",
      "message": "scan 0: 2 weight elements (3103, 3202); exactly one 310n/320n is required",
      "offset": 26,
      "expected": "exactly one weight AI 310n (kg) or 320n (lb)",
      "found": "3103, 3202"
    }
  }
}
```

## 效期优先拣货计划 `POST /weight-pick-plan`

一次提交多条扫码字符串和一组按 GTIN 的需求，按 **FEFO（先到期先出）** 为每个
需求分配净重，并按每个需求的剩余效期门槛过滤库存。

- 请求体：`{"scans": [...], "target_unit": "kg", "as_of": "2025-01-15", "demands": [{"gtin": "...", "quantity": 5, "min_remaining_days": 30}]}`。
  `target_unit` 可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`as_of` **必填**，为
  `YYYY-MM-DD` 格式的真实日期（计划基准日），缺失或非法返回 `422`；`scans` 中的
  每一项既可以是元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫描必须包含**恰好一个 AI 01**、**恰好一个 AI 17**（效期）、**零个或一个
  AI 10**（批号可缺省）和**恰好一个重量 AI**（310n 或 320n，允许混合千克/磅）；
  AI 21 会被忽略。任一条扫描解析失败或缺少/重复必需 AI，整个请求返回 `422`，
  `detail.scan_index` 给出出错下标（从 0 开始），`detail.error` 为该条扫描的原始
  错误（结构同 `/parse`）。
- `demands` 每项含 `gtin` 与 `quantity`（`quantity` 以 `target_unit` 计，必须大于 0，
  按 `Decimal` 精确解析，JSON number 与字符串均可），以及可选的
  `min_remaining_days`（**非负整数，默认 0**）和 `package_mode`
  （`"splittable"` / `"whole"`，**默认 `"splittable"`**）。**同一 GTIN 重复出现返回
  `422`**，`detail.demand_index` 给出出错下标；`min_remaining_days` 为负数时同样
  返回 `422` 并以 `detail.demand_index` 定位。
- 效期门槛：对每个需求，仅**实际效期不早于 `as_of + min_remaining_days` 天**的
  库存分组可参与分配（边界日期当天可用）；AI 17 日字段 `00` 仍按当月最后一天
  解释（如 `250200` = 2025-02-28，闰年 `240200` = 2024-02-29）。被门槛排除的库存
  **不计入 `fulfilled`**，缺口仍计入 `shortage`。
- 汇总与分配（`package_mode: "splittable"`，默认）：先按 **GTIN + 批号 + AI 17 原值**
  汇总可用重量（换算到 `target_unit`），再对每个需求的 GTIN 在**合格**分组中按
  **AI 17 实际日期升序**执行 FEFO；实际日期相同的分组保持批次**首次出现**顺序；
  **最后一批允许部分分配**。
- 整包模式（`package_mode: "whole"`）：把该需求 GTIN 的**每条合格扫描**视为一个
  **不可拆包裹**（不做分组汇总），沿用实际效期、保质期门槛与 `Decimal` 单位换算。
  合格包裹按 FEFO（实际效期升序，同日保持原扫描顺序）排序后，若存在总重达到
  `quantity` 的子集，选取**超量最小**者；超量相同时选取**索引序列字典序最小**者
  （索引即 FEFO 排序后的位置）；若任何子集都达不到 `quantity`，则**选取全部合格
  包裹**。单个需求的合格包裹**超过 30 个**时整个请求返回 `422`
  （`detail.demand_index` 定位，`detail.error.error` 为 `TOO_MANY_PACKAGES`）。
- 换算、汇总、比较与分配全程使用 `Decimal` 与固定换算系数
  **1 lb = 0.45359237 kg**（千克→磅按其精确倒数换算），仅在响应时按**六位小数
  half-even（银行家舍入）**输出为 JSON 数字（如 `2.513000`）。
- 响应：`target_unit` 加与请求同序的 `demands` 数组。每项含 `gtin`、`quantity`
  （回显）、`fulfilled`（已分配总量）、`shortage`（缺口，**不为负**，整包超量时为
  `0.000000`）、`overage`（整包模式超出 `quantity` 的部分，可拆模式恒为
  `0.000000`）、`excluded_weight`（同 GTIN 因门槛被排除的重量合计，以
  `target_unit` 计，无排除时为 `0.000000`）与 `allocations`。可拆模式下
  `allocations` 按分配顺序、每条对应一个分组（`batch`、`expiry`（AI 17 原值
  YYMMDD）与 `weight`）；整包模式下**每条对应一个入选扫描**，按 FEFO 顺序返回。
  `scans` 为空数组时每个需求都是全额 `shortage`（`fulfilled` 为 `0.000000`，
  `allocations` 为空）。

请求示例（三个批次效期不同，需求 12 kg，目标单位 kg，`as_of` 之前无库存到期）：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "as_of": "2025-01-01", "scans": ["01095011015300031725033110LATE\u001d3103002000", "01095011015300031725013110EARLY\u001d3103002000", "01095011015300031725022810MID\u001d3202002513"], "demands": [{"gtin": "09501101530003", "quantity": 12}]}'
```

`EARLY`（2025-01-31，2 kg）最先到期全部分配，`MID`（2025-02-28，25.13 lb ≈
11.398776 kg）部分分配 10 kg，`LATE` 不动；响应 `200 OK`：

```json
{
  "target_unit": "kg",
  "demands": [
    {
      "gtin": "09501101530003",
      "quantity": 12.000000,
      "fulfilled": 12.000000,
      "shortage": 0.000000,
      "overage": 0.000000,
      "excluded_weight": 0.000000,
      "allocations": [
        {"batch": "EARLY", "expiry": "250131", "weight": 2.000000},
        {"batch": "MID",   "expiry": "250228", "weight": 10.000000}
      ]
    }
  ]
}
```

效期门槛示例：`as_of` 为 2025-02-10、`min_remaining_days` 为 10，门槛即
2025-02-20——`OLD`（2025-01-31 到期）被排除并计入 `excluded_weight`，
`FRESH`（2025-02-28 到期）合格并参与分配：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"as_of": "2025-02-10", "scans": ["01095011015300031725013110OLD\u001d3103002000", "01095011015300031725022810FRESH\u001d3103002000"], "demands": [{"gtin": "09501101530003", "quantity": 2, "min_remaining_days": 10}]}'
```

```json
{
  "target_unit": "kg",
  "demands": [
    {
      "gtin": "09501101530003",
      "quantity": 2.000000,
      "fulfilled": 2.000000,
      "shortage": 0.000000,
      "overage": 0.000000,
      "excluded_weight": 2.000000,
      "allocations": [
        {"batch": "FRESH", "expiry": "250228", "weight": 2.000000}
      ]
    }
  ]
}
```

库存不足时缺口计入 `shortage`（空 `scans` 即全额短缺）：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"as_of": "2025-01-15", "scans": [], "demands": [{"gtin": "09501101530003", "quantity": 2.5}]}'
```

```json
{"target_unit": "kg", "demands": [{"gtin": "09501101530003", "quantity": 2.500000, "fulfilled": 0.000000, "shortage": 2.500000, "overage": 0.000000, "excluded_weight": 0.000000, "allocations": []}]}
```

整包模式示例：三条扫描分别为 2 kg（`P1`，2025-01-31 到期）、3 kg（`P2`，
2025-02-28 到期）、4 kg（`P3`，2025-03-31 到期），需求 5 kg。达到 5 kg 的子集中
`{P1, P2}` 恰好 5 kg（超量 0），优于 `{P1, P3}`（超量 1）与 `{P2, P3}`（超量 2），
故选中 `P1`、`P2` 两个包裹，按 FEFO 顺序返回：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"as_of": "2025-01-01", "scans": ["01095011015300031725013110P1\u001d3103002000", "01095011015300031725022810P2\u001d3103003000", "01095011015300031725033110P3\u001d3103004000"], "demands": [{"gtin": "09501101530003", "quantity": 5, "package_mode": "whole"}]}'
```

```json
{
  "target_unit": "kg",
  "demands": [
    {
      "gtin": "09501101530003",
      "quantity": 5.000000,
      "fulfilled": 5.000000,
      "shortage": 0.000000,
      "overage": 0.000000,
      "excluded_weight": 0.000000,
      "allocations": [
        {"batch": "P1", "expiry": "250131", "weight": 2.000000},
        {"batch": "P2", "expiry": "250228", "weight": 3.000000}
      ]
    }
  ]
}
```

同一请求把 `quantity` 改为 `6.5` 时，没有恰好 6.5 kg 的子集，超量最小的是
`{P2, P3}`（7 kg，超量 0.5 kg）：响应中 `fulfilled` 为 `7.000000`，`overage` 为
`0.500000`，`shortage` 为 `0.000000`（不为负）。若需求超过全部合格包裹总重，则
选取全部合格包裹，缺口留在 `shortage`，`overage` 为 `0.000000`。

单个需求的合格包裹超过 30 个时返回 `422`：

```json
{
  "detail": {
    "demand_index": 0,
    "error": {
      "error": "TOO_MANY_PACKAGES",
      "message": "demand 0: 31 eligible packages for GTIN '09501101530003' exceed the whole-package limit of 30",
      "expected": "at most 30 eligible packages per whole-package demand",
      "found": "31"
    }
  }
}
```

扫描错误与 `/weight-summary` 同形（`detail.scan_index` + 原始错误）；需求中 GTIN
重复时返回 `422`：

```json
{
  "detail": {
    "demand_index": 1,
    "error": {
      "error": "DUPLICATE_GTIN",
      "message": "demand 1: GTIN '09501101530003' is already requested by demand 0; each GTIN may appear only once",
      "expected": "unique GTIN across all demands",
      "found": "09501101530003"
    }
  }
}
```

`min_remaining_days` 为负数时同样返回 `422`，`detail.demand_index` 定位出错需求：

```json
{
  "detail": {
    "demand_index": 1,
    "error": {
      "error": "INVALID_MIN_REMAINING_DAYS",
      "message": "demand 1: min_remaining_days must be a non-negative integer, got -3",
      "expected": "non-negative integer min_remaining_days",
      "found": "-3"
    }
  }
}
```

## 召回范围盘点 `POST /recall-impact`

一次提交多条扫码字符串和一组**有序召回规则**，按 **GTIN + 序列号（AI 01 + AI 21）**
识别实物，统计每条规则命中的包裹数与总净重，并汇总未命中任何规则的实物。

- 请求体：`{"scans": [...], "target_unit": "kg", "rules": [{"gtin": "...", "batches": ["B1"], "expiry_from": "250101", "expiry_to": "250228"}]}`。
  `target_unit` 可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`scans` 中的每一项既可以是
  元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫描必须包含**恰好一个 AI 01**、**恰好一个 AI 17**（效期）、**恰好一个 AI 21**
  （序列号）、**零个或一个 AI 10**（批号可缺省）和**恰好一个重量 AI**（310n 或 320n，
  允许混合千克/磅）。任一条扫描解析失败或缺少/重复必需 AI，整个请求返回 `422`，
  `detail.scan_index` 给出出错下标（从 0 开始），`detail.error` 为该条扫描的原始错误
  （结构同 `/parse`）。
- 实物识别与去重：**GTIN + 序列号**唯一确定一个实物。同一实物被重复扫描且批号、
  效期、重量完全一致时**只计一次**（效期按 AI 17 原值比较；重量按单位相同且数值
  精确相等判断，例如 `3103 002513` 与 `3104 025130` 同为 2.513 kg）。若同一实物的
  两次扫描在**批号、效期或重量**上不一致，整个请求返回 `422`：`detail.scan_index`
  为当前（冲突）扫描下标，`detail.first_index` 为该实物首次出现的下标，
  `detail.error.error` 为 `CONFLICTING_BATCH` / `CONFLICTING_EXPIRY` /
  `CONFLICTING_WEIGHT`。
- 规则字段：`gtin` 必填（仅作匹配条件，不校验校验位）；`batches` 可选批号集合——
  省略时匹配任意批号，给定时实物的 AI 10 必须在集合内（集合中的 `null` 匹配无批号
  实物；空集合不匹配任何实物）；`expiry_from` / `expiry_to` 可选效期闭区间边界，
  格式为 6 位 YYMMDD（日 `00` 表示当月最后一天），按**实际日期**比较、两端皆含，
  可只给一端。边界不是 6 位数字、不是真实日期或 `expiry_from` 晚于 `expiry_to`
  时返回 `422`，`detail.rule_index` 定位出错规则（错误码 `INVALID_EXPIRY_BOUND` /
  `INVALID_DATE` / `EXPIRY_RANGE_INVERTED`）。规则校验先于扫描解析。
- 匹配：实物按规则**原序**归入**首条**命中的规则；未命中任何规则的实物计入
  `unmatched`。
- 响应：`target_unit`、与请求同序的 `rules` 数组和 `unmatched`。每条规则回显
  `rule_index`、`gtin`、`batches`、`expiry_from`、`expiry_to`，并给出 `package_count`
  （命中实物数）、`unit` 与 `total_weight`；`unmatched` 给出未命中实物的
  `package_count`、`unit` 与 `total_weight`。所有总重全程 `Decimal` 计算（换算系数
  **1 lb = 0.45359237 kg**），仅在响应时按**六位小数 half-even（银行家舍入）**输出
  为 JSON 数字（如 `2.513000`）。
- `scans` 为空时所有规则与 `unmatched` 均为 `0` / `0.000000`；`rules` 为空时全部
  实物计入 `unmatched`。

请求示例（`SN2` 被重复扫描且属性一致只计一次；`SN3` 批号不在集合内而未命中）：

```bash
curl -X POST http://localhost:8080/recall-impact \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "scans": ["01095011015300031725013110B1\u001d21SN1\u001d3103002000", "01095011015300031725022810B2\u001d21SN2\u001d3103003000", "01095011015300031725022810B2\u001d21SN2\u001d3103003000", "01095011015300031725033110B9\u001d21SN3\u001d3202002513"], "rules": [{"gtin": "09501101530003", "batches": ["B1", "B2"], "expiry_from": "250101", "expiry_to": "250228"}]}'
```

响应 `200 OK`：规则命中 `SN1`（2 kg）与 `SN2`（3 kg）共 2 包 5 kg；`SN3`
（25.13 lb ≈ 11.398776 kg）未命中：

```json
{
  "target_unit": "kg",
  "rules": [
    {
      "rule_index": 0,
      "gtin": "09501101530003",
      "batches": ["B1", "B2"],
      "expiry_from": "250101",
      "expiry_to": "250228",
      "package_count": 2,
      "unit": "kg",
      "total_weight": 5.000000
    }
  ],
  "unmatched": {"package_count": 1, "unit": "kg", "total_weight": 11.398776}
}
```

同一实物两次扫描属性冲突时返回 `422`（当前下标 1 与首次下标 0 一并给出）：

```json
{
  "detail": {
    "scan_index": 1,
    "first_index": 0,
    "error": {
      "error": "CONFLICTING_BATCH",
      "message": "scan 1: item GTIN '09501101530003' serial 'SN1' was first scanned at index 0 with batch 'B1', but this scan has batch 'B2'",
      "expected": "batch 'B1' (scan 0)",
      "found": "batch 'B2'"
    }
  }
}
```

规则效期区间倒置时返回 `422`，`detail.rule_index` 定位出错规则：

```json
{
  "detail": {
    "rule_index": 0,
    "error": {
      "error": "EXPIRY_RANGE_INVERTED",
      "message": "rule 0: expiry_from '250331' (2025-03-31) is after expiry_to '250131' (2025-01-31)",
      "expected": "expiry_from on or before expiry_to",
      "found": "250331 > 250131"
    }
  }
}
```

## 追溯事件审计 `POST /trace-audit`

一次提交一组**按发生顺序排列**的追溯事件，按 **GTIN + 序列号（AI 01 + AI 21）**
识别实物，逐项执行 **receive → pick → ship** 状态机，记录每项事件前后的状态与异常，
并按最终状态汇总实物数量。

- 请求体：`{"events": [{"type": "receive", "scan": "..."}]}`。`events` 按事件发生
  顺序排列；`type` 仅接受 `"receive"` / `"pick"` / `"ship"`（其他值返回 `422`）；
  `scan` 既可以是元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫描必须包含**恰好一个 AI 01**、**恰好一个 AI 21**（序列号）、**零个或一个
  AI 10**（批号可缺省）和**零个或一个 AI 17**（效期可缺省）；出现的重量 AI
  （310n/320n）会被忽略。任一条扫描解析失败或缺少/重复必需 AI，整个请求返回
  `422`，`detail.event_index` 给出出错事件下标（从 0 开始），`detail.error` 为该条
  扫描的原始错误（结构同 `/parse`）。
- 实物识别与一致性：**GTIN + 序列号**唯一确定一个实物。实物的批号（AI 10 原值，
  缺省也算一种取值）与效期（AI 17 原值，缺省同理）由它的**首个事件**确定；后续
  事件的批号或效期不一致时整个请求返回 `422`：`detail.event_index` 为当前（冲突）
  事件下标，`detail.first_index` 为该实物首个事件的下标，`detail.error.error` 为
  `CONFLICTING_BATCH` / `CONFLICTING_EXPIRY`。
- 状态机：每个实物沿 `none`（尚无已完成阶段）→ `received` → `picked` →
  `shipped` 推进，只有恰好命中下一阶段的事件才推进状态；其余事件**记录异常且
  不推进状态**，异常码如下：
  - `FIRST_EVENT_NOT_RECEIVE`：实物尚未 receive 就收到 pick/ship（首个事件不是
    receive）；
  - `DUPLICATE_STAGE`：重复刚完成的阶段（如第二次 receive、第二次 ship）；
  - `STAGE_REGRESSION`：回退到更早的阶段（如 pick 之后又 receive、ship 之后又
    pick）；
  - `SKIPPED_STAGE`：跳过未完成的阶段（如未 pick 直接 ship）。
- 响应：`events` 按输入顺序逐项给出 `event_index`、`type`、`gtin`、`serial`、
  `state_before`、`state_after` 与 `anomaly`（事件推进状态时为 `null`，否则为含
  `error` / `message` / `expected` / `found` 的异常对象）；`summary` 汇总各最终
  状态的实物数量（`none` / `received` / `picked` / `shipped` 四项恒在）。
- `events` 为空数组时返回 `200`，`events` 为空、`summary` 各项为 0。

请求示例（`SN1` 走完整流程；`SN2` 重复 receive 记录异常；`SN3` 未收货先拣货）：

```bash
curl -X POST http://localhost:8080/trace-audit \
  -H "Content-Type: application/json" \
  -d '{"events": [{"type": "receive", "scan": "01095011015300031725013110B1\u001d21SN1"}, {"type": "pick", "scan": "01095011015300031725013110B1\u001d21SN1"}, {"type": "ship", "scan": "01095011015300031725013110B1\u001d21SN1"}, {"type": "receive", "scan": "01095011015300031725022810B2\u001d21SN2"}, {"type": "receive", "scan": "01095011015300031725022810B2\u001d21SN2"}, {"type": "pick", "scan": "010950110153000321SN3"}]}'
```

响应 `200 OK`：前三项依次推进 `none → received → picked → shipped`；第 4 项
`SN2` 正常收货，第 5 项重复 receive 记录 `DUPLICATE_STAGE` 且状态停留
`received`；第 6 项 `SN3` 的首个事件是 pick，记录 `FIRST_EVENT_NOT_RECEIVE`，
状态停留 `none`：

```json
{
  "events": [
    {"event_index": 0, "type": "receive", "gtin": "09501101530003", "serial": "SN1", "state_before": "none",     "state_after": "received", "anomaly": null},
    {"event_index": 1, "type": "pick",    "gtin": "09501101530003", "serial": "SN1", "state_before": "received", "state_after": "picked",   "anomaly": null},
    {"event_index": 2, "type": "ship",    "gtin": "09501101530003", "serial": "SN1", "state_before": "picked",   "state_after": "shipped",  "anomaly": null},
    {"event_index": 3, "type": "receive", "gtin": "09501101530003", "serial": "SN2", "state_before": "none",     "state_after": "received", "anomaly": null},
    {"event_index": 4, "type": "receive", "gtin": "09501101530003", "serial": "SN2", "state_before": "received", "state_after": "received",
     "anomaly": {"error": "DUPLICATE_STAGE", "message": "event 4: item GTIN '09501101530003' serial 'SN2' is already 'received'; the 'receive' stage was already completed", "expected": "the next event 'pick'", "found": "'receive'"}},
    {"event_index": 5, "type": "pick",    "gtin": "09501101530003", "serial": "SN3", "state_before": "none",     "state_after": "none",
     "anomaly": {"error": "FIRST_EVENT_NOT_RECEIVE", "message": "event 5: item GTIN '09501101530003' serial 'SN3' has not been received, but got a 'pick' event; the first event of an item must be 'receive'", "expected": "the next event 'receive'", "found": "'pick'"}}
  ],
  "summary": {"none": 1, "received": 1, "picked": 0, "shipped": 1}
}
```

同一实物不同事件的批号或效期不一致时返回 `422`（当前下标与首次下标一并给出）：

```json
{
  "detail": {
    "event_index": 1,
    "first_index": 0,
    "error": {
      "error": "CONFLICTING_BATCH",
      "message": "event 1: item GTIN '09501101530003' serial 'SN1' was first seen at event 0 with batch 'B1', but this event has batch 'B2'",
      "expected": "batch 'B1' (event 0)",
      "found": "batch 'B2'"
    }
  }
}
```

扫描解析或结构错误与 `/weight-summary` 同形，`detail.event_index` 定位出错事件：

```json
{
  "detail": {
    "event_index": 0,
    "error": {
      "error": "MISSING_AI",
      "message": "event 0: missing required AI 21 (serial number)",
      "expected": "exactly one AI 01 (GTIN), exactly one AI 21 (serial number), at most one AI 10 (batch/lot) and at most one AI 17 (expiry date)"
    }
  }
}
```

## 生产批次谱系审计 `POST /batch-genealogy`

一次提交**期初批次库存**和一组**顺序任意**的转换工序，按 **GTIN + 批号（AI 01 + AI 10）**
标识批次：先校验生产/消耗依赖图，再按**稳定拓扑序**逐步扣减库存，返回工序谱系、各工序
投入/产出/损耗重量与全部批次的最终余量。

- 请求体：`{"opening_stock": [...], "processes": [{"inputs": [...], "outputs": [...]}], "target_unit": "kg"}`。
  `target_unit` 可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`opening_stock` 为期初库存
  扫码数组；`processes` 为工序数组（顺序任意），每道工序含 `inputs`（消耗）与
  `outputs`（产出）两个扫码数组（字段必填，可为空数组）。所有扫码值既可以是元素串，
  也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫码必须包含**恰好一个 AI 01**、**恰好一个 AI 10**（批号必填——批次由
  GTIN+批号标识）和**恰好一个重量 AI**（310n 或 320n，允许混合千克/磅）；AI 17/21
  会被忽略。任一条扫码解析失败或缺少/重复必需 AI，整个请求返回 `422`：期初库存出错时
  `detail.scan_index` 给出下标；工序扫码出错时 `detail.process_index` 给出工序下标，
  并以 `detail.input_index` / `detail.output_index` 给出工序内下标；`detail.error`
  为该条扫码的原始错误（结构同 `/parse`）。
- 重量一律使用 `Decimal` 从**原始六位数字与精度位 n** 重建，按固定系数
  **1 lb = 0.45359237 kg** 换算到 `target_unit` 后按 **GTIN+批号** 聚合：期初库存
  聚合一次，每道工序的投入、产出分别聚合。
- 依赖图校验（在扣减任何库存之前；出错均整单返回 `422` 并携带工序下标）：
  - **一批多产**：同一批次最多由一道工序产出。第二道产出该批次的工序使整单失败，
    `detail.process_index` 为当前工序、`detail.first_index` 为首次产出的工序
    （错误码 `MULTIPLE_PRODUCERS`）。
  - **无来源批次**：工序消耗的批次必须来自期初库存或某道工序的产出，否则整单失败，
    `detail.process_index` 定位出错工序（错误码 `UNKNOWN_BATCH`）。
  - **循环依赖**：工序消耗另一工序产出的批次即构成依赖边，依赖图必须无环（工序
    消耗自身产出也算长度为 1 的环）。检测到环时整单失败，`detail.process_index`
    与 `detail.cycle` 给出环上的工序下标（错误码 `CYCLE_DETECTED`）。
- 库存扣减：按**稳定拓扑序**执行工序——在所有投入均已就绪的工序中，**请求下标最小
  者先执行**；每道工序先按批次扣减投入、再记入产出。任一道工序对某批次的投入超过
  当前可用余量（超耗）时整单返回 `422`，`detail.process_index` 定位出错工序（错误码
  `INSUFFICIENT_STOCK`）。同一批次既在期初库存又由工序产出时，数量累加。
- 响应：`target_unit`、`processes` 与 `batches`。`processes` 按**稳定拓扑序**排列，
  每项含 `process_index`（请求中的下标）、按批次聚合的 `inputs` / `outputs`（各含
  `gtin`、`batch`、`weight`，批次按工序内首次出现排序）以及 `input_weight` /
  `output_weight` / `loss_weight`（损耗 = 投入 − 产出，产出大于投入时为负）。
  `batches` 按首次出现顺序（期初库存在前，其后按请求顺序各工序的产出）给出全部批次的
  `opening` / `produced` / `consumed` / `remaining` 重量。所有重量全程 `Decimal`
  计算，仅在响应时按**六位小数 half-even（银行家舍入）**输出为 JSON 数字
  （如 `2.513000`）。
- `opening_stock` 与 `processes` 均为空数组时返回 `200` 与空的 `processes` / `batches`。

请求示例（10 kg 原料批次 `R1`；工序 1 消耗 6 kg `R1` 产出 5.5 kg 成品批次 `P1`，
工序 0 再消耗 2 kg `P1` 产出 1.9 kg 批次 `P2`——请求中工序顺序颠倒，仍按拓扑序执行）：

```bash
curl -X POST http://localhost:8080/batch-genealogy \
  -H "Content-Type: application/json" \
  -d '{"opening_stock": ["010950110153000310R1\u001d3103010000"], "processes": [{"inputs": ["010000000000001710P1\u001d3103002000"], "outputs": ["010000000000001710P2\u001d3103001900"]}, {"inputs": ["010950110153000310R1\u001d3103006000"], "outputs": ["010000000000001710P1\u001d3103005500"]}]}'
```

响应 `200 OK`：`processes` 按稳定拓扑序排列（工序 1 先于工序 0），`batches` 给出
三个批次的最终余量（`R1` 余 4 kg、`P1` 余 3.5 kg、`P2` 余 1.9 kg）：

```json
{
  "target_unit": "kg",
  "processes": [
    {
      "process_index": 1,
      "inputs": [{"gtin": "09501101530003", "batch": "R1", "weight": 6.000000}],
      "outputs": [{"gtin": "00000000000017", "batch": "P1", "weight": 5.500000}],
      "input_weight": 6.000000,
      "output_weight": 5.500000,
      "loss_weight": 0.500000
    },
    {
      "process_index": 0,
      "inputs": [{"gtin": "00000000000017", "batch": "P1", "weight": 2.000000}],
      "outputs": [{"gtin": "00000000000017", "batch": "P2", "weight": 1.900000}],
      "input_weight": 2.000000,
      "output_weight": 1.900000,
      "loss_weight": 0.100000
    }
  ],
  "batches": [
    {"gtin": "09501101530003", "batch": "R1", "opening": 10.000000, "produced": 0.000000, "consumed": 6.000000, "remaining": 4.000000},
    {"gtin": "00000000000017", "batch": "P2", "opening": 0.000000, "produced": 1.900000, "consumed": 0.000000, "remaining": 1.900000},
    {"gtin": "00000000000017", "batch": "P1", "opening": 0.000000, "produced": 5.500000, "consumed": 2.000000, "remaining": 3.500000}
  ]
}
```

同一批次由两道工序产出时返回 `422`（当前工序下标与首次产出下标一并给出）：

```json
{
  "detail": {
    "process_index": 1,
    "first_index": 0,
    "error": {
      "error": "MULTIPLE_PRODUCERS",
      "message": "process 1: batch GTIN '00000000000017' batch 'P1' is already produced by process 0; each batch may be produced by at most one process",
      "expected": "batch produced only by process 0",
      "found": "second producer at process 1"
    }
  }
}
```

消耗无来源批次（既不在期初库存也不由任何工序产出）时返回 `422`：

```json
{
  "detail": {
    "process_index": 0,
    "error": {
      "error": "UNKNOWN_BATCH",
      "message": "process 0: input batch GTIN '09501101530003' batch 'ZZ' has no source: it is neither in the opening stock nor produced by any process",
      "expected": "input batch present in opening_stock or produced by exactly one process",
      "found": "GTIN '09501101530003' batch 'ZZ'"
    }
  }
}
```

依赖成环时返回 `422`，`cycle` 给出环上的工序下标：

```json
{
  "detail": {
    "process_index": 0,
    "cycle": [0, 1],
    "error": {
      "error": "CYCLE_DETECTED",
      "message": "processes 0 -> 1 -> 0 form a production/consumption dependency cycle; the graph must be acyclic",
      "expected": "an acyclic producer/consumer dependency graph",
      "found": "cycle 0 -> 1 -> 0"
    }
  }
}
```

某道工序超耗（投入超过当前可用余量）时返回 `422`，需求量与可用量一并给出：

```json
{
  "detail": {
    "process_index": 0,
    "error": {
      "error": "INSUFFICIENT_STOCK",
      "message": "process 0: batch GTIN '09501101530003' batch 'R1' requires 6.000000 kg but only 4.000000 kg is available",
      "expected": "at most 4.000000 kg of batch GTIN '09501101530003' batch 'R1'",
      "found": "6.000000 kg"
    }
  }
}
```

## 错误响应

解析或校验失败返回 `422 Unprocessable Entity`，`detail` 中包含错误码、
字节偏移（`offset`，UTF-8 字节序）与预期规则（`expected`）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "0109501101530004"}'
```

```json
{
  "detail": {
    "error": "INVALID_CHECK_DIGIT",
    "message": "GTIN check digit mismatch: expected 3, found 4",
    "offset": 15,
    "expected": "Mod-10 check digit 3",
    "found": "4"
  }
}
```

错误码一览：

| 错误码                 | 说明                                                          |
| ---------------------- | ------------------------------------------------------------- |
| `EMPTY_INPUT`          | 输入为空（元素串为空，或 URI 路径/查询中没有任何 AI/值对）    |
| `UNEXPECTED_SEPARATOR` | 在应出现 AI 的位置遇到分隔符（仅元素串）                      |
| `INVALID_AI`           | 不是合法的数字 AI（元素串为非 2 位数字；URI 为非数字段/键）   |
| `UNSUPPORTED_AI`       | 不支持的 AI（仅支持 01/10/17/21 及 310n/320n）                |
| `INVALID_PRECISION`    | 310/320 前缀后缺少或给出非数字的精度位 `n`（须为 0-9）        |
| `FIELD_TOO_SHORT`      | 固定长度字段字符不足（含重量字段不足 6 位数字）               |
| `FIELD_EMPTY`          | 可变长度字段为空                                              |
| `FIELD_TOO_LONG`       | 可变长度字段超过 20 字符（或固定长度字段超长，仅 URI）        |
| `INVALID_CHARACTER`    | 字符不符合该字段字符集                                        |
| `INVALID_CHECK_DIGIT`  | GTIN Mod-10 校验位不匹配                                      |
| `INVALID_DATE`         | 效期不是真实日期                                              |
| `UNPAIRED_PATH`        | Digital Link URI 路径段不是成对的 AI/值（仅 URI）             |
| `DUPLICATE_AI`         | AI 重复出现，或同时出现在路径与查询参数中（仅 URI）           |
| `INVALID_PERCENT_ENCODING` | `%XX` 不是合法十六进制转义或不是合法 UTF-8（仅 URI）      |

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest                 # 运行测试
uvicorn app.main:app --reload --port 8080
```

## 项目结构

```
app/
  main.py     # FastAPI 应用与请求/响应模型（/parse、/weight-summary、/weight-pick-plan、/recall-impact、/trace-audit、/batch-genealogy）
  gs1.py      # GS1 元素串 / Digital Link URI 解析与校验核心
  summary.py  # 多扫描重量汇总：Decimal 重建、单位换算、GTIN+批号分组
  pickplan.py # FEFO 拣货计划：GTIN+批号+效期汇总、as_of+min_remaining_days 效期门槛、按实际效期分配需求；whole 整包模式按单条扫描做最小超量子集选择
  recall.py   # 召回范围盘点：GTIN+序列号实物去重与冲突检测、有序规则（GTIN+批号集合+效期闭区间）首命中归组、未命中汇总
  traceaudit.py # 追溯事件审计：GTIN+序列号实物识别、批号/效期一致性校验、receive→pick→ship 状态机与异常记录、最终状态汇总
  genealogy.py # 生产批次谱系审计：GTIN+批号批次台账、一批一产/无来源/循环依赖图校验、稳定拓扑序库存扣减与超耗拒绝、批次最终余量
tests/
  test_parse.py
  test_weight_summary.py
  test_weight_pick_plan.py
  test_recall_impact.py
  test_trace_audit.py
  test_batch_genealogy.py
Dockerfile
docker-compose.yml
requirements.txt      # 运行依赖
requirements-dev.txt  # 测试依赖
```
