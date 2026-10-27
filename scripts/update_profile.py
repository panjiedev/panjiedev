#!/usr/bin/env python3
"""读取 GitHub 公开数据，生成个人主页；只依赖标准库与 gh CLI。"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from html import escape
import json
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
BEIJING = timezone(timedelta(hours=8))
NAME = re.compile(r"[A-Za-z0-9_.-]+")
THEMES = {
    "light": {"bg": "#ffffff", "border": "#d1d9e0", "text": "#1f2328", "muted": "#59636e", "link": "#0969da", "green": "#1a7f37", "track": "#eaeef2"},
    "dark": {"bg": "#0d1117", "border": "#3d444d", "text": "#e6edf3", "muted": "#9198a1", "link": "#58a6ff", "green": "#3fb950", "track": "#21262d"},
}
LANG_COLORS = {"Java": "#b07219", "TypeScript": "#3178c6", "Python": "#3572a5", "JavaScript": "#b39700", "CSS": "#835bb5", "HTML": "#d84b2a", "Go": "#007d9c", "Shell": "#598116", "Rust": "#936348", "其他": "#7d8590"}
FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Noto Sans,PingFang SC,Microsoft YaHei,sans-serif"


def repository_name(value: str) -> str:
    parts = value.split("/")
    if len(parts) != 2 or not all(NAME.fullmatch(p) for p in parts):
        raise ValueError("项目名称必须为 owner/repo。")
    return value


class GitHub:
    def __init__(self):
        self.repositories = {}

    def get(self, path, **params):
        if params:
            path += "?" + urlencode(params)
        for attempt in range(3):
            try:
                result = subprocess.run(
                    ["gh", "api", "--hostname", "github.com", "--method", "GET", path],
                    capture_output=True, text=True, timeout=60, check=True,
                )
                return json.loads(result.stdout)
            except (subprocess.SubprocessError, json.JSONDecodeError) as error:
                if attempt == 2:
                    # 不把 gh 的完整诊断或认证上下文写入公开日志。
                    raise RuntimeError(f"GitHub 公开数据读取失败：{path.split('?')[0]}") from error
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def public_repository(self, name):
        name = repository_name(name)
        if name not in self.repositories:
            data = self.get(f"repos/{name}")
            if data.get("private") is not False or data.get("visibility", "public") != "public":
                raise ValueError("仅允许公开项目。")
            if data.get("full_name", "").casefold() != name.casefold():
                raise ValueError("GitHub 项目身份不一致。")
            self.repositories[name] = data
        return self.repositories[name]


def paginated(api, path, **params):
    for page in range(1, 10001):
        values = api.get(path, **params, per_page=100, page=page)
        if not isinstance(values, list):
            raise ValueError("GitHub 列表响应不完整。")
        yield from values
        if len(values) < 100:
            return
    raise ValueError("列表分页超出保护范围。")


def merged_pull_requests(api, username, now):
    base = f"is:pr author:{username} is:merged is:public"

    def search(query, lower, upper):
        first = api.get("search/issues", q=query, per_page=100, page=1)
        if first.get("incomplete_results"):
            raise ValueError("PR 搜索返回不完整结果，保留旧快照。")
        total = first["total_count"]
        if total > 1000:
            if lower >= upper:
                raise ValueError("单秒内的 PR 搜索超过窗口，不能可靠统计。")
            middle = (lower + upper) // 2
            items = []
            for start, end in ((lower, middle), (middle + 1, upper)):
                iso = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                items.extend(search(f"{base} created:{iso(start)}..{iso(end)}", start, end))
            return items
        items = list(first["items"])
        for page in range(2, math.ceil(total / 100) + 1):
            response = api.get("search/issues", q=query, per_page=100, page=page)
            if response.get("incomplete_results") or response.get("total_count") != total:
                raise ValueError("PR 搜索在分页期间发生变化，请重试。")
            items.extend(response["items"])
        if len(items) != total or len({i["id"] for i in items}) != total:
            raise ValueError("PR 分页缺失或重复，请重试。")
        return items

    items = search(base, 0, int(now.timestamp()))
    groups = defaultdict(list)
    for item in items:
        prefix = "https://api.github.com/repos/"
        url = item.get("repository_url", "")
        if not url.startswith(prefix):
            raise ValueError("PR 来源地址异常。")
        name = repository_name(url[len(prefix):])
        if name.split("/")[0].casefold() == username.casefold():
            continue
        api.public_repository(name)
        groups[name].append({"number": item["number"], "url": f"https://github.com/{name}/pull/{int(item['number'])}"})
    return [
        {"repository": name, "merged_prs": len(prs), "stars": api.public_repository(name)["stargazers_count"], "pull_requests": sorted(prs, key=lambda p: p["number"], reverse=True)}
        for name, prs in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0].casefold()))
    ]


def language_percentages(counts):
    if not counts:
        return []
    rows = sorted(counts.items(), key=lambda p: (-p[1], p[0]))
    if len(rows) > 6:
        rows = rows[:5] + [("其他", sum(value for _, value in rows[5:]))]
    total = sum(value for _, value in rows)
    raw = [value * 1000 / total for _, value in rows]
    units = [math.floor(value) for value in raw]
    # 最大余数法：一位小数的展示合计为 100%，不丢弃长尾语言。
    for index in sorted(range(len(raw)), key=lambda i: -(raw[i] - units[i]))[:1000 - sum(units)]:
        units[index] += 1
    return [{"name": name, "bytes": value, "percent": units[i] / 10} for i, (name, value) in enumerate(rows)]


def collect(api, config, now):
    username = config["username"]
    if not NAME.fullmatch(username):
        raise ValueError("GitHub 用户名不合法。")
    contributions = merged_pull_requests(api, username, now)
    totals, included = Counter(), []
    for repo in paginated(api, f"users/{username}/repos", type="owner", sort="full_name"):
        name = repo["name"]
        if (repo.get("private") is not False or repo.get("fork") or repo.get("archived") or not repo.get("size")
                or repo.get("owner", {}).get("login", "").casefold() != username.casefold()
                or name.casefold() == username.casefold()
                or any(fnmatchcase(name.casefold(), pattern.casefold()) for pattern in config["language_exclude_patterns"])):
            continue
        full_name = repository_name(repo["full_name"])
        languages = api.get(f"repos/{full_name}/languages")
        if not isinstance(languages, dict) or any(not isinstance(v, int) or v < 0 for v in languages.values()):
            raise ValueError("语言字节数响应异常。")
        if sum(languages.values()):
            totals.update(languages)
            included.append(full_name)
    featured = []
    for selected in config["featured"]:
        repo = api.public_repository(selected["repository"])
        featured.append({"repository": repo["full_name"], "role": selected["role"], "description": selected.get("description") or repo.get("description") or "",
                         "language": repo.get("language"), "stars": repo["stargazers_count"], "pushed_at": repo["pushed_at"],
                         "merged_prs": next((r["merged_prs"] for r in contributions if r["repository"].casefold() == repo["full_name"].casefold()), 0)})
    return {"schema_version": 1, "username": username, "generated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data_scope": "仅公开 GitHub 数据", "contributions": contributions,
            "languages": {"total_bytes": sum(totals.values()), "included_repositories": sorted(included), "bytes": dict(sorted(totals.items())), "shares": language_percentages(totals)},
            "featured": featured}


def svg_start(width, height, theme, title, description):
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" font-family="{FONT}">',
            f'<title>{escape(title)}</title><desc>{escape(description)}</desc>',
            f'<rect x="0.5" y="0.5" width="{width-1}" height="{height-1}" rx="8" fill="{theme["bg"]}" stroke="{theme["border"]}"/>']


def text(x, y, value, color, size=13, weight=400, anchor="start"):
    return f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}">{escape(str(value))}</text>'


def truncate(value, limit):
    return value if len(value) <= limit else value[:limit - 1] + "…"


def wrap(value, limit=30, max_lines=3):
    lines, current, units = [], "", 0
    for char in value.replace("\n", " "):
        width = 1 if ord(char) > 0x2e80 else .55
        if current and units + width > limit:
            lines.append(current)
            current, units = "", 0
            if len(lines) == max_lines:
                lines[-1] = lines[-1].rstrip() + "…"
                return lines
        current += char
        units += width
    if current:
        lines.append(current)
    return lines


def contribution_svg(data, theme, limit):
    rows = data["contributions"][:limit]
    total = sum(row["merged_prs"] for row in data["contributions"])
    height = 95 + max(1, len(rows)) * 42
    out = svg_start(880, height, theme, "开源贡献", f"{total} 个外部项目已合并 PR；Star 是项目本身的当前 Star 数。")
    out += [text(20, 30, "开源贡献", theme["text"], 17, 600), text(860, 30, f"{total} 个已合并 PR · {len(data['contributions'])} 个项目", theme["muted"], 12, anchor="end"),
            text(20, 60, "项目", theme["muted"], 12), text(738, 60, "合并 PR", theme["muted"], 12, anchor="end"), text(860, 60, "项目 Star", theme["muted"], 12, anchor="end")]
    maximum = max((r["merged_prs"] for r in rows), default=1)
    for i, row in enumerate(rows):
        y = 85 + i * 42
        out += [text(20, y + 2, truncate(row["repository"], 32), theme["link"], 13, 500),
                f'<rect x="280" y="{y-9}" width="380" height="12" rx="6" fill="{theme["track"]}"/>',
                f'<rect x="280" y="{y-9}" width="{380 * row["merged_prs"] / maximum:.2f}" height="12" rx="6" fill="{theme["green"]}"/>',
                text(738, y + 2, row["merged_prs"], theme["text"], 13, 600, "end"), text(860, y + 2, f"{row['stars']:,}", theme["text"], 13, anchor="end")]
    if not rows:
        out.append(text(20, 87, "暂无符合统计口径的已合并 PR", theme["muted"]))
    out.append(text(20, height - 15, "仅公开外部项目；完整 PR 链接见贡献明细", theme["muted"], 11))
    return "".join(out) + "</svg>\n"


def languages_svg(data, theme):
    rows = data["languages"]["shares"]
    height = 145 + 25 * math.ceil(len(rows) / 3)
    out = svg_start(880, height, theme, "公开代码语言占比", "按 GitHub 语言字节数汇总；排除 Fork、课程练习、空仓库和主页自身。")
    out += [text(20, 31, "公开代码语言占比", theme["text"], 17, 600), text(860, 31, "按代码字节数汇总", theme["muted"], 12, anchor="end")]
    out.append('<defs><clipPath id="language-bar"><rect x="20" y="55" width="840" height="13" rx="6"/></clipPath></defs><g clip-path="url(#language-bar)">')
    x = 20
    for row in rows:
        width = 840 * row["percent"] / 100
        color = LANG_COLORS.get(row["name"], "#7d8590")
        out.append(f'<rect x="{x:.2f}" y="55" width="{width:.2f}" height="13" fill="{color}"/>')
        x += width
    out.append("</g>")
    for i, row in enumerate(rows):
        x, y = 20 + (i % 3) * 280, 98 + (i // 3) * 25
        out += [f'<circle cx="{x+4}" cy="{y-4}" r="4" fill="{LANG_COLORS.get(row["name"], "#7d8590")}"/>',
                text(x + 16, y, row["name"], theme["text"], 12), text(x + 250, y, f"{row['percent']:.1f}%", theme["muted"], 12, anchor="end")]
    if not rows:
        out.append(text(20, 85, "暂无可统计的公开代码", theme["muted"]))
    out += [text(20, height - 37, f"{len(data['languages']['included_repositories'])} 个公开自有代码仓库；不代表技能熟练程度", theme["muted"], 11),
            text(20, height - 17, "排除 Fork、课程练习、空仓库和主页自身", theme["muted"], 11)]
    return "".join(out) + "</svg>\n"


def project_svg(project, theme):
    out = svg_start(440, 185, theme, project["repository"], project["description"])
    label = project["repository"].split("/")[1] if project["role"] == "自有项目" else project["repository"]
    out += [text(17, 29, truncate(label, 29), theme["link"], 15, 600), text(423, 50, project["role"], theme["muted"], 11, anchor="end")]
    for i, line in enumerate(wrap(project["description"], limit=31, max_lines=3)):
        out.append(text(17, 73 + i * 21, line, theme["muted"], 12))
    bottom = f"{project.get('language') or '公开项目'}"
    if project["role"] == "社区贡献":
        bottom = f"{project['merged_prs']} 个 PR 已合并"
    out += [text(17, 166, bottom, theme["green"] if project["role"] == "社区贡献" else theme["muted"], 12), text(423, 166, f"Star {project['stars']:,}", theme["text"], 12, 500, "end")]
    return "".join(out) + "</svg>\n"


def picture(username, name, alt, width="100%", version=""):
    base = f"https://raw.githubusercontent.com/{username}/{username}/main/assets/{name}"
    base += f"-{version}" if version else ""
    return f'<picture>\n  <source media="(prefers-color-scheme: dark)" srcset="{base}-dark.svg">\n  <img src="{base}-light.svg" alt="{escape(alt, quote=True)}" width="{width}">\n</picture>'


def render_outputs(config, data):
    outputs = {}
    for name, theme in THEMES.items():
        outputs[f"assets/contributions-{name}.svg"] = contribution_svg(data, theme, config["max_contribution_rows"])
        outputs[f"assets/languages-{name}.svg"] = languages_svg(data, theme)
        for i, project in enumerate(data["featured"]):
            outputs[f"assets/project-{i+1}-{name}.svg"] = project_svg(project, theme)
    versions = {}
    for key in ["contributions", "languages"] + [f"project-{i+1}" for i in range(len(data["featured"]))]:
        versions[key] = hashlib.sha256((outputs[f"assets/{key}-light.svg"] + outputs[f"assets/{key}-dark.svg"]).encode()).hexdigest()[:16]
        for theme in THEMES:
            outputs[f"assets/{key}-{versions[key]}-{theme}.svg"] = outputs.pop(f"assets/{key}-{theme}.svg")
    stamp = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00")).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M UTC+8")
    username = config["username"]
    readme = ["<!-- 此文件由 scripts/update_profile.py 生成；简介及项目请修改 profile.json。 -->", f"# 你好，我是 {username}", "", config["introduction"], "", " · ".join(config["topics"]), "", "## 开源贡献", "", picture(username, "contributions", "公开外部项目合并 PR 与项目 Star 统计", version=versions["contributions"]), "", "<details>", "<summary>查看贡献明细</summary>", "", "| 项目 | 已合并 PR | 项目 Star |", "| --- | --- | ---: |"]
    for row in data["contributions"]:
        name = row["repository"]
        links = "、".join(f"[#{p['number']}]({p['url']})" for p in row["pull_requests"])
        readme.append(f"| [{name}](https://github.com/{name}) | {links} | {row['stars']:,} |")
    if not data["contributions"]:
        readme.append("| 暂无符合统计口径的记录 | — | — |")
    readme += ["", "仅统计公开外部项目已合并的 PR，排除本人仓库。Star 为该项目本身的数量。", "", "</details>", "", "## 公开代码语言占比", "", picture(username, "languages", "公开代码按 GitHub Languages API 字节数汇总的语言占比", version=versions["languages"]), "", "<details>", "<summary>统计范围</summary>", "", "当前纳入：" + ("、".join(f"[`{repo}`](https://github.com/{repo})" for repo in data["languages"]["included_repositories"]) or "暂无"), "", "排除 Fork、已归档、空仓库、主页自身及 `skills-*` 课程练习。语言占比基于公开代码字节数，不代表技能熟练程度。", "", "</details>", "", "## 项目与实践", "", "<div align=\"center\">"]
    for i, project in enumerate(data["featured"]):
        name = project["repository"]
        readme.append(f'<a href="https://github.com/{name}">\n{picture(username, f"project-{i+1}", name + " · " + project["role"] + " · Star " + str(project["stars"]), "400", version=versions[f"project-{i+1}"])}\n</a>')
    readme += ["</div>", "", f"<sub>仅公开 GitHub 数据 · 更新于 {stamp} · 每日北京时间 00:30 自动刷新 · [维护说明](MAINTAINING.md)</sub>", ""]
    outputs["README.md"] = "\n".join(readme)
    outputs["assets/data.json"] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return outputs


def update(root=ROOT, api=None, now=None):
    root = Path(root)
    config = json.loads((root / "profile.json").read_text())
    data = collect(api or GitHub(), config, now or datetime.now(timezone.utc))
    outputs = render_outputs(config, data)
    # 全部 API 和渲染成功后才准备写入；工作流只提交成功退出的结果。
    with tempfile.TemporaryDirectory(prefix=".profile-build-", dir=root) as temp:
        for name, content in outputs.items():
            target = Path(temp, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for name in outputs:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(Path(temp, name), target)
    # 仅清理本生成器的旧图片；文件保留在 Git 历史中。
    for path in (root / "assets").glob("*.svg"):
        if re.fullmatch(r"(?:contributions|languages|project-\d+)(?:-[0-9a-f]{16})?-(?:light|dark)\.svg", path.name) and f"assets/{path.name}" not in outputs:
            path.unlink()
    print(f"公开数据已更新：{sum(r['merged_prs'] for r in data['contributions'])} 个合并 PR / {len(data['contributions'])} 个外部项目；{len(data['languages']['included_repositories'])} 个语言统计仓库。")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        update(args.root)
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"主页更新失败，未提交新的统计快照：{error}", flush=True)
        raise SystemExit(1)
