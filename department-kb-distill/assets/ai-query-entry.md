# {{KNOWLEDGE_BASE_NAME}}｜AI问答与检索入口

> Claude、Codex 或其他 Agent 通过 DWS 使用本知识库时，先读本页；本页只规定检索路径，不代替业务原文。

## 知识库信息

- 知识库：{{KNOWLEDGE_BASE_NAME}}
- 索引内容截至：{{GENERATED_AT}}
- 成功知识页：{{DOCUMENT_COUNT}} 份
- [打开AI知识库地图]({{KNOWLEDGE_MAP_LINK}})
- [按业务分类浏览]({{BUSINESS_INDEX_LINK}})
- [按原目录浏览]({{RAW_INDEX_LINK}})

## Agent固定路径

1. 用户给的是蒸馏后Wiki根目录时，先用 `dws doc list --folder "<根目录nodeId>" --page-size 50 --format json` 找到本页；记录该根目录nodeId。
2. 用 `dws doc info` 读取本页，取得当前 `workspaceId` 和 `folderId`；`folderId`必须等于本次蒸馏后Wiki根目录nodeId。
3. 用 `dws doc search --query "<问题关键词>" --workspace-ids "<workspaceId>" --limit 10 --format json` 召回候选；用户已给具体文档链接时跳过搜索。
4. 对每个搜索候选用 `dws doc info` 沿 `folderId` 向上检查父目录；祖先链不包含本次根目录nodeId的候选必须丢弃，避免命中同一workspace里的旧版或其他知识库。
5. 对保留候选执行 `dws doc block list --node "<nodeId>" --content-format jsonml --start-index 0 --end-index 1 --format json`，只读页首“AI检索元数据（17字段与蒸馏画像）”。
6. 按 `stable_id` 合并两套目录中的重复结果，默认保留业务分类视图。
7. 对筛选出的相关文档执行 `dws doc read`，回到 `Original Content/Attachment` 核对事实。简单问题通常读取1–3份；跨流程、对比、汇总或冲突问题按需分批扩展，直到关键信息覆盖完整，不得因数量限制遗漏相关依据。
8. 需要补充上下文时，优先沿已确认的关联文档扩展；输出结论时附文档名称、当前状态、更新时间和可点击链接。

## 使用边界

- 页首统一AI检索元数据只用于筛选，制度、数字、时间、责任和原句必须回到完整原文。
- `候选`、`待确认` 不表示已形成正式口径；`已失效` 只用于历史核对。
- 发现冲突时分别列出双方原文，不替业务负责人裁定。
- 不得通过索引、稳定ID或关联关系泄露无权访问的文档及其存在性。
- DWS搜索按workspace召回，不天然限制在当前发布根目录；第4步的根目录归属检查不能省略。
- 问“有哪些资料”、要求浏览目录、根目录过滤后无结果时，再读AI知识库地图或目录索引。
