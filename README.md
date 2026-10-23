# spt-mod-monitor（Alife 技能）

监控 The Forge（sp-mod.com）上 SPT（单机塔科夫）的新 MOD 发布与近期更新。用法、退出码口径全在 **[SKILL.md](SKILL.md)**。

- 装法：解压到 `Alife\Storage\Skills\spt-mod-monitor\`（就是这个目录本体）
- 这站没有拦截，脚本**自己抓取、自己翻页**（每页 12 条，翻到时间窗被覆盖为止），不用浏览器、不用接收器
- 一条命令跑完：`py -3 scripts\spt_monitor.py --update`
- 全程零回显：结果写 `Storage\SkillData\spt_report.txt`，状态一行写 `spt_status.txt`，失败写 `spt_error.txt`，靠退出码带信号（`0 无变化 / 1 有变化 / 2 抓到但解析不出卡片 / 3 抓取失败`）
- 判更新只认卡片 `<time datetime>` 的绝对时间严格晚于缓存值；页面自带 RSS 但 `pubDate` 是首次发布时间，会整页误报，**不要用**
- 缓存与产物都在 `Storage\SkillData\`，不在本仓库里
- 离线自检：`py -3 scripts/spt_monitor.py --selftest --print`（不联网、不碰真缓存）
