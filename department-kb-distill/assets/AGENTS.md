# 知识库问答执行规则

本文件用于将知识库接入Claude、Codex等可调用DWS的Agent。

## 知识库信息

- 知识库名称：`<KNOWLEDGE_BASE_NAME>`
- 蒸馏后根目录：`<DISTILLED_WIKI_ROOT_URL>`
- AI检索入口：`<AI_QUERY_ENTRY_URL>`

## 固定只读路径

1. 若只拿到蒸馏后根目录链接，先用 `dws doc list` 找到`00-AI问答与检索入口`；记录根目录nodeId。
2. 用 `dws doc info --node "<AI_QUERY_ENTRY_URL>" --format json` 取得当前 `workspaceId`，并确认返回的`folderId`等于根目录nodeId。
3. 用 `dws doc search --query "<问题关键词>" --workspace-ids "<workspaceId>" --limit 10 --format json` 召回候选。用户已给具体文档链接时跳过搜索。
4. 对搜索候选用`dws doc info`沿`folderId`向上检查父目录，只保留祖先链包含当前根目录nodeId的页面；同一workspace中其他或旧版根目录的页面全部丢弃。
5. 对保留候选执行 `dws doc block list --node "<nodeId>" --content-format jsonml --start-index 0 --end-index 1 --format json`，只读页首“AI检索元数据（17字段与蒸馏画像）”。
6. 按 `stable_id` 合并双目录中的同一来源，默认保留业务分类视图。
7. 对筛选出的相关页面执行 `dws doc read`，回到 `Original Content/Attachment` 核对事实。简单问题通常读取1–3页；跨流程、对比、汇总或冲突问题按需分批扩展，直到关键信息覆盖完整，不得因数量限制遗漏相关依据。需要上下文时优先沿已通过关联扩展。
8. 回答附文档标题、页面状态、更新时间和可点击链接。

## 索引使用

- 日常问答优先DWS搜索，但必须完成当前根目录归属过滤，不先读全量目录。
- 问“有哪些资料”、需要浏览业务范围或过滤后无结果时，再读`00-AI知识库地图`或目录索引。
- 索引是导航和备用召回，不是事实结论。

## 事实与权限边界

- 页首统一AI检索元数据只用于筛选；规则、数字、时间、责任和原句必须核对完整原文。
- `候选`、`待确认`不表示当前正式口径；`已失效`只用于历史核对。
- 发现冲突时分别引用双方原文，不替业务负责人裁定。
- 不得绕过钉钉原有权限，也不得通过索引或关联关系泄露无权文档的存在。
- 默认只读；任何创建、修改、发布、移动、删除或改权限都需要当前任务明确授权。
