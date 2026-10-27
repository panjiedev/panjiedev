# 主页维护

本仓库的根目录 README 是 GitHub 个人主页。简介、项目卡片及统计排除项在 `profile.json` 中维护；`README.md` 和 `assets/` 由脚本生成，不手动修改统计值。

## 数据口径

- 开源贡献：公开且已合并的 PR，排除本人仓库，按目标项目汇总。Star 是目标项目本身的当前 Star 数，与贡献者个人获得的 Star 无关。
- 语言占比：公开、非 Fork、未归档、非空的本人仓库，按 GitHub Languages API 返回的代码字节数汇总；排除主页仓库和 `skills-*` 课程练习。不代表技能熟练程度。
- 项目卡片：由 `featured` 明确选择，角色区分自有项目和社区贡献；Star 数从项目 API 获取。
- PR 和仓库列表完整分页；PR 搜索超过 1,000 条时按创建时间分段，拒绝将不完整搜索结果作为总数。

## 自动更新

`.github/workflows/update-profile.yml` 每天 `16:30 UTC` 触发，即北京时间次日 `00:30`，也支持在 Actions 页面手动运行。GitHub 定时任务可能排队延迟；展示的时间始终是实际成功生成时间。

修改配置、脚本、测试或工作流后会自动验证和更新。工作流使用仓库自带的 `GITHUB_TOKEN`，仅发布任务具有本仓库 `contents: write` 权限，无需额外 PAT，也不读取私有项目数据。

脚本先完成全部获取和素材生成，再写入输出；获取或生成失败会使任务失败，不提交半成品，不用示例值替代统计。主页继续保留上次成功提交；可由 Actions 状态和快照时间判断新鲜度。

## 本地检查

需要 Python 3.10+ 和已登录 GitHub 的 `gh`，没有额外 Python 依赖。

```bash
python3 -m unittest discover -s tests -v
python3 scripts/update_profile.py
git diff --check
```

本地更新只读取公开 API 并写本地文件，不自动推送。`assets/data.json` 是本次 SVG 和 README 使用的同一份快照。

图片文件名带有内容哈希版本，避免 GitHub 图片缓存造成的文字与图表统计暂时不一致；只清理本生成器的旧图片，历史版本仍保留在 Git 中。
