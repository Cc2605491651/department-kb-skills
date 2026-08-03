# 蒸馏后Codex问答与DWS检索规范

## 1. 核心原则

日常问答采用“DWS搜索候选 → 当前发布根目录过滤 → 页首统一AI检索元数据初筛 → 少量全文核对”。不遍历全库，不依赖巨大全量索引，不只看摘要下结论。DWS搜索的workspace范围可能同时包含旧版发布根目录，所以根目录过滤不能省略。

## 2. 标准路径

1. 用户给出蒸馏后Wiki根目录或`00-AI问答与检索入口`链接。
2. 只有根目录链接时，先用`dws doc list`找到`00-AI问答与检索入口`；记录根目录nodeId。
3. Agent用`dws doc info`读取入口，取得当前`workspaceId`，并确认入口`folderId`等于根目录nodeId。
4. 用`dws doc search`在该workspace召回最多10个候选；用户已给具体文档链接时跳过搜索。
5. 对每个搜索候选用`dws doc info`沿`folderId`向上查父目录，只保留祖先链包含当前根目录nodeId的页面。
6. 对保留候选执行`dws doc block list --start-index 0 --end-index 1 --content-format jsonml`，只读页首“AI检索元数据（17字段与蒸馏画像）”。
7. 按`stable_id`合并两套目录中的重复页，默认保留业务分类视图。
8. 对筛选出的相关页执行`dws doc read`核对完整原文。简单问题通常读取1–3页；跨流程、对比、汇总或冲突问题按需分批扩展，直到关键信息覆盖完整，不得因数量限制遗漏相关依据；必要时优先沿已通过关联扩展。
9. 回答附文档名称、状态、更新时间和链接。

## 3. DWS典型命令

```bash
dws doc info --node "<AI_QUERY_ENTRY_URL>" --format json

dws doc search \
  --query "<问题关键词>" \
  --workspace-ids "<WORKSPACE_ID>" \
  --limit 10 \
  --format json

# 对搜索结果及其父文件夹逐级执行，直到命中当前发布根目录nodeId或离开该目录树
dws doc info --node "<CANDIDATE_OR_PARENT_NODE_ID>" --format json

dws doc block list \
  --node "<CANDIDATE_NODE_ID>" \
  --content-format jsonml \
  --start-index 0 \
  --end-index 1 \
  --format json

dws doc read --node "<SELECTED_NODE_ID>" --format json
```

`dws doc info`只返回钉钉原生节点信息，不会自动返回页面正文中的17字段或语义画像；所以需要用block list读取页首统一区块。该区块同时返回17字段和蒸馏画像，不需要再读取第二套检索卡。

## 4. 索引的作用

- `00-AI知识库地图`：选择业务范围，不列全量文档。
- `目录索引`：浏览某个文件夹的直接子目录和文档。
- 日常问答优先DWS搜索并过滤到当前发布根目录；只有问“有哪些资料”、需要浏览范围或过滤后无结果时才读索引。

## 5. 事实、状态与关联

- 页首统一AI检索元数据只用于初筛。
- 数字、日期、制度、责任人和原句必须回到完整原文。
- `正式`可作为当前已确认资料；`候选`仅作线索；`待确认`不得当作定论；`已失效`只用于历史分析。
- 只沿已通过关联扩展；待负责人确认的关联不得作为唯一结论依据。
- 冲突时并列双方原文与状态，不替业务负责人裁定。

## 6. 权限与写入

DWS继续遵守当前用户在钉钉的权限。不得通过索引、稳定ID或关联关系泄露无权文档的标题、摘要或存在性。问答默认只读；任何写入都需要当前任务明确授权并回读。
