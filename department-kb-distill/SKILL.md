---
name: department-kb-distill
description: 面向所有部门的钉钉知识库首次全量蒸馏与全量重建技能。用于新知识库启动、只读盘点、Raw格式准入、文档下载解析、Codex摘要与语义画像、统一AI检索元数据、R01–R10关系召回、L2自动通过与抽样、L3业务确认、双目录页面、AI问答入口、执行结果矩阵，以及向任务启动时指定的钉钉文件夹发布并回读。独立图片、音频、视频、压缩包、扫描PDF和纯图片PPT直接排除；已有蒸馏知识库的日常增量发现、局部蒸馏和健康检查使用department-kb-maintain。
---

# 部门知识库蒸馏

把每份准入材料生成两套目录视图中的完整知识页，并保留17字段、Raw Mirror、相关知识、完整原文及可审计台账。

## 必读顺序

开始任何新任务前读取：

1. `references/11-新知识库启动信息清单.md`
2. `references/raw-admission-standard.md`
3. `references/01-全量蒸馏执行总规范.md`

按当前阶段补充读取：

- 获取与解析：`references/03-来源获取下载与解析执行规范.md`、`references/format-compatibility-matrix.md`
- 页面、17字段和摘要：`references/02-双目录页面与17字段规范.md`、`references/04-Codex摘要与语义画像规范.md`
- 关系候选、核验和名词：`references/05-文档关系规则与审核分级规范.md`、`references/10-名词解释与规则对照.md`
- 审核、发布、回读和验收：`references/06-业务确认发布回读与归档规范.md`、`references/08-全流程验收清单.md`
- 本地交付与下一步：`references/12-执行结果与下一步交付规范.md`
- 角色协作：`references/09-角色分工与部门协作规范.md`
- 蒸馏后问答：仅在建设问答工作区时读取 `references/07-蒸馏后Codex问答与DWS检索规范.md`

不得跳过 `scripts/preflight.py`。编排器会在每个阶段自动运行门禁；缺少配置、规范、上游台账或发布目标不一致时必须停止。

## 新任务初始化

从 Skill 目录外执行，使用绝对路径最稳妥：

```bash
python3 <SKILL_ROOT>/scripts/bootstrap_job.py \
  --job <JOB_DIR> \
  --task-id <TASK_ID> \
  --department <DEPARTMENT> \
  --workspace-id <SOURCE_WORKSPACE_ID> \
  --workspace-url <SOURCE_WORKSPACE_URL> \
  --workspace-name <SOURCE_WORKSPACE_NAME> \
  --id-prefix <STABLE_ID_PREFIX> \
  --executed-by <AI_TASK_INITIATOR> \
  --publish-target <FINAL_DINGTALK_FOLDER_URL> \
  --publish-root-name <NEW_ROOT_FOLDER_NAME>
```

不需要写回线上时省略 `--publish-target` 和 `--publish-root-name`。初始化后补全 `00-config/department-taxonomy.yaml`、`sensitive-scope.yaml` 和 `task-config.yaml` 中仍存在的占位符，再运行：

```bash
python3 <SKILL_ROOT>/scripts/run_pipeline.py --job <JOB_DIR> --stage preflight
```

## 执行主流程

1. 用DWS只读盘点来源空间，保留节点、原路径、创建者UID/姓名、时间、链接和权限快照。`Owner`固定等于钉钉原文创建者姓名；目录接口缺失时必须自动执行“`doc search`按workspace和标题召回 → nodeId精确匹配 → `contact user get`把creatorUid换成姓名”，仍不可得才写“待补充”，不得从正文人名推测。
2. 先执行Raw准入。独立图片、音频、视频、压缩包和白名单外文件进入排除清单，不进入下载解析、Codex或成功/失败统计。扫描PDF和纯图片PPT在内容门禁中失败并给出补件建议。
3. 下载或读取准入材料，校验文件头、大小和SHA-256；第一次解析失败后使用兼容方式重试，仍失败则保留原件并登记真实原因。
4. 让Codex生成摘要、场景、关键词和内容特征；本地程序只负责切片、缓存、Schema校验和来源画像拼装。
5. 用R01–R10召回候选，再让Codex双端核验方向、关系类型、通俗解释、双方证据和风险。R09/R10不得单独成为正式关系。
6. L0/L1自动通过；L2在双方证据完整且无L3风险时自动通过并进入事后抽样；冲突、废止/替代效力、权限泄露或制度效力判断统一进入L3逐条确认。
7. 生成原目录视图和业务分类视图。每份成功文档在两边都必须包含文档标题、页首统一AI检索元数据（内含固定17字段与蒸馏画像）、Raw Mirror、相关知识及Original Content/Attachment；只允许目录层级和`view`不同。不得再生成独立“AI检索卡”“标准元数据”或“蒸馏画像”区块。完整原文中的附件临时签名参数必须清除，能定位时改链父文档。
8. 生成根目录AI问答入口、轻量知识库地图、每层目录索引、逐文档结果矩阵和待办动作清单。
9. 运行本地验收。未通过时不得发布。

本地验收通过后，只有本次任务执行审核人在AI对话框明确表达“确认此次蒸馏没有问题”“验收通过”等同等含义，Agent才执行整批确认：

```bash
python3 <SKILL_ROOT>/scripts/run_pipeline.py \
  --job <JOB_DIR> \
  --stage confirm \
  --confirmation-text "<执行审核人的确认原话>" \
  --confirmed-by "<执行审核人姓名>"
```

确认前成功文档的`status`为“候选”；确认后，本批内容哈希未变化且Owner已取得创建者姓名的成功文档改为“正式”。AI不能自行确认，含糊评价和仅确认个别问题不触发整批状态变更。来源内容变化后，哈希不再匹配确认记录，对应文档自动回到“候选”。

首次建议逐阶段执行；稳定后可运行不含线上写入的本地全流程：

```bash
python3 <SKILL_ROOT>/scripts/run_pipeline.py --job <JOB_DIR> --stage all --workers 4
```

`all` 永远不自动发布、不自动同步AI表格。AI表格同步是单独授权能力，发布目录链接不包含该权限。

## 与增量维护Skill的边界

首次建设、全量重建和全量规则升级使用本Skill。已有蒸馏任务的新增、修改、移动、权限变化、原文失联、孤立页面和新冲突检查使用同级`department-kb-maintain`。

本Skill为增量维护提供稳定接口：`inventory_wiki.py --output-dir`生成独立只读快照；`run_pipeline.py --source-id`只抽取并生成指定稳定ID的语义画像；关系候选仍在本地全量重建，但有效缓存保证未变化关系不重复调用Codex。不得在增量Skill中复制本Skill的Raw、画像、关系、页面或发布规则。

## 发布与回读

任务启动时在 `task-config.yaml` 设置 `publishing.enabled: true` 并填写最终 `target_folder_url`，即授权本任务在该目录新建并更新本程序创建的节点；无需额外授权JSON。没有该链接时只能本地处理。

先烟雾测试，再全量发布和回读：

```bash
python3 <SKILL_ROOT>/scripts/run_pipeline.py --job <JOB_DIR> --stage publish --smoke-groups 2 --publish-workers 4
python3 <SKILL_ROOT>/scripts/run_pipeline.py --job <JOB_DIR> --stage readback --last-scope --publish-workers 4
python3 <SKILL_ROOT>/scripts/run_pipeline.py --job <JOB_DIR> --stage publish --publish-workers 30
python3 <SKILL_ROOT>/scripts/run_pipeline.py --job <JOB_DIR> --stage readback --publish-workers 30
```

命令行 `--target` 仅可重复任务配置中的同一目标；不一致时立即停止。正式发布包含两套知识视图、各层目录索引、根目录AI问答入口和知识库地图；不上传本地审核、失败、配置或缓存文件。发布始终禁止删除、移动、改权限和覆盖非本程序创建的节点。单次Markdown片段最多9,000字符，并发和每批任务最多30；回读同时检查页首统一AI检索元数据、正文、链接、内容哈希、末尾和远程目录树。

若整批确认发生在首次发布前，后续发布直接写入“正式”；若确认发生在已发布之后，必须再次执行`publish`和`readback`，线上页面的状态才会同步为“正式”。

## 蒸馏后问答路径

Agent不遍历整库，也不依赖巨大的全量索引。DWS搜索只支持workspace范围，同一workspace可能存在旧版发布目录，因此必须再做当前根目录归属过滤：

1. 先读根目录`00-AI问答与检索入口`；
2. DWS在目标workspace搜索候选；
3. 用`doc info`沿候选的`folderId`向上检查，只保留祖先链包含本次蒸馏后Wiki根目录nodeId的页面；
4. 只读保留候选页首0–1号block的统一AI检索元数据（17字段与蒸馏画像）；
5. 按稳定ID合并双目录重复；
6. 对筛选出的相关页读全文；简单问题通常读取1–3页，跨流程、对比、汇总或冲突问题按需分批扩展，直到关键信息覆盖完整，不得因数量限制遗漏相关依据；必要时优先沿已通过关联展开。

目录索引和AI知识库地图用于浏览、范围选择和搜索无结果时的备用路径。

## 结束任务前的强制交付

不得只生成本地文档就结束对话。最终回复前必须读取`00-蒸馏结果与下一步.md`和待办清单，直接在用户与AI的对话框输出：

1. 本次处理总数、成功、排除和失败数；
2. 当前状态是“仅本地蒸馏”、“仍待处理”还是“已发布并全量回读通过”；
3. 必须处理的下一步、责任角色和数量；
4. 结果矩阵、待办清单、本地审核入口和线上Wiki的可点击链接。

只要P0/P1待办不为0，最终回复必须明确写“仍待处理，未全部结束”，不得只写“已完成”。本地`00-蒸馏结果与下一步.md`继续保留，用于后续复核和移交，但不能代替对话交付。

## 固定规则

- 稳定ID使用 `SHA-256(workspace_id + ":" + node_id)` 前12位大写十六进制；移动、改名和正文更新不变，删除重建会变化。
- 相同内容只共享内容画像，不共享路径、Owner、权限和更新时间。
- 不根据标题、目录、人名或常识补造正文事实。
- `relations: []` 表示没有通过发布门槛的关系，不代表现实中一定没有关系。
- 一对多关系逐边保存，展示时按来源文档和关系类型聚合。
- 关系展示取两端运行时权限交集，不泄露受限文档的标题、链接、摘要或存在性。
- 所有Codex调用记录输入哈希、提示词/Schema版本、CLI版本、尝试次数、状态和用量。
