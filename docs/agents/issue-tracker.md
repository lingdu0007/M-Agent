# Issue Tracker：本地 Markdown

本仓库的 Issue 和 PRD 以 Markdown 文件形式保存在 `.scratch/` 下。

## 约定

- 每个功能使用一个目录：`.scratch/<feature-slug>/`
- PRD 路径为 `.scratch/<feature-slug>/PRD.md`
- 实现 Issue 路径为 `.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 开始编号
- 分诊状态记录在每个 Issue 文件顶部附近的 `Status:` 行中
- 评论和对话历史追加在文件底部的 `## Comments` 标题下

## 当技能要求“发布到 Issue Tracker”时

在 `.scratch/<feature-slug>/` 下创建新文件；如果目录不存在，则一并创建。

## 当技能要求“获取相关 Ticket”时

读取被引用的本地文件。用户通常会直接提供文件路径或 Issue 编号。
