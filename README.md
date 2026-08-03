# 部门知识库Skills

用于钉钉部门知识库的首次全量蒸馏和后续增量维护。

## 包含内容

- `department-kb-distill`：首次全量蒸馏、审核、发布与回读。
- `department-kb-maintain`：发现新增或修改内容，执行局部蒸馏和健康检查。

## 首次安装

先克隆仓库：

```bash
git clone https://github.com/Cc2605491651/department-kb-skills.git
cd department-kb-skills
```

让AI确认当前Agent的个人Skill目录，然后执行：

```bash
python3 install.py --target "<个人Skill目录>" --mode link
```

常见目录示例：

- 通用Agent目录：`~/.agents/skills`
- Codex个人目录：`~/.codex/skills`
- Claude Code个人目录：`~/.claude/skills`

安装后如果Agent没有立即识别，请重启Agent。

## 获取更新

使用软链接安装时，只需要在本仓库执行：

```bash
git pull --ff-only
```

Agent使用的Skill会同步更新。

如果使用复制模式安装，更新后需要重新覆盖：

```bash
python3 install.py --target "<个人Skill目录>" --mode copy --replace
```

被替换的旧版本会自动备份，不会直接删除。

## 使用顺序

1. 首次建设知识库时使用 `department-kb-distill`。
2. 首次蒸馏验收并上线后，再使用 `department-kb-maintain` 设置定期检查。

每个Skill的具体执行要求以对应目录中的 `SKILL.md` 为准。
