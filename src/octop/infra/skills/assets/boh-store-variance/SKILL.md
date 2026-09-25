---
name: BOH 门店分差分析
description: 当用户询问当前门店的库存分差、盘盈盘亏、标准原物料、周考核物料、周趋势或指定物料逐日异常时使用。通过 BOH 连接器按需取原始数据，再由可信分析工具精确计算。
---

# BOH 门店分差分析

只分析当前会话选择器中的一家门店。若未选门店，先请用户选择；不要根据提问中的门店名称擅自切换。不要请求、显示或转述掌柜/BOH token。BOH 工具的身份、门店和权限由服务端确定。

## 时间与取数

- 用户指定日期时遵循用户日期。未指定时，先调用 `get_boh_default_period`，采用其返回的服务器时区、开始和结束日期；每月 1 日会得到上一个完整自然月。不要把未来日期当成已发生数据。
- 按问题选择报表，不必每次查齐四张：库存汇总用 `query_store_cos_page`，标准原物料用 `query_store_generic_page`，周考核物料用 `query_store_assessment_week_page`，周标准原物料用 `query_store_generic_week_page`。
- 库存汇总和标准原物料默认传 `financeCategoryNames=["食材成本"]`；用户明确要求其他财务类别或全部时才改变。两张周报不支持此筛选，必须与已筛选的报表并列展示，标注“未传财务类别筛选”，不把金额跨报表相加。
- 工具返回单页完整原始 `records`、`total`、`pageIndex`、`pageSize`，周报另有 `headers`。依 `total` 和 `pageSize` 逐页调用；后续页传第一页返回的 `datasetId`，日期、筛选和页长保持一致。若结果过大，缩短日期或调小 `pageSize`，重新开始一个数据集。任一页缺失时不可宣称总计。
- 指定物料分析先调用 `search_raw_items`，按编码确认 `rawItemIds`；同名多编码先列出候选请用户确认，不能随意合并。再用 `query_store_cos_daily` 逐日取原始记录。最多 31 天，需更长时间时拆段并分别说明。

## 精确计算与报告

- 原始页取齐后调用 `analyze_boh_variance`，传当前会话的 `datasetIds`、模式 `overview`、同一日期范围。指定物料逐日趋势用模式 `daily`，传逐日查询返回的所有 `datasetId`。它只做确定性计算，不访问 BOH；若报告缺页、条件不一致或门店变化，应重查，不手算替代。
- 报表中的 `usageDiffCost` 为分差金额；正数解释为盘亏，负数为盘盈。标准原物料的 `lossDiffCost` 单独展示。周报依据 `weekDataMap` 的周/月字段解释趋势，不与库存汇总金额相加。不同单位的数量不能相加。
- 先给结论、门店与日期口径，再展示可用报表的合计、TOP/BOTTOM、周趋势或逐日异常。逐日无记录标记“无数据”，不可写成零。分析结果中的 `unavailableReports` 是无权限报表，`notQueriedReports` 是本次未查询；不要把两者混为一谈。只取得部分报表时仍分析已有部分，并说明不能得出的结论。
- 数字以分析工具结果为准；模型负责业务解释、风险提示和建议，不在长上下文中重新求和。首版在对话中交付，不生成 CSV、Excel 或 HTML。
