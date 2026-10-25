"""云端（WorkBuddy 沙箱 / 任意无 X 网络的环境）侧：读取 feed → 出候选清单

用途：X 抓取只能在能出网的机器上做。那台机器把 feed.jsonl 推到 GitHub（或任何
可访问的 HTTPS 地址）后，这里把增量拉下来，做关键词过滤 + 去重 + 摘要，产出：
  - candidates.json   给下一步 P2 评分 / 逆向用
  - digest.md         给人看的一段简报

用法：
  python collect.py --url https://raw.githubusercontent.com/<user>/<repo>/main/feed.jsonl \
                    --since 48 --out-dir out
  python collect.py --file /path/feed.jsonl          # 本地文件（调试用）
  python collect.py --url ... --seen seen.json --dry-run

沙箱里 x.com 不通，但 raw.githubusercontent.com 通，所以走这条路。
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.request

KEYWORDS = re.compile(
    r"free|免费|白嫖|no signup|unlimited|open source|开源|playground|beta|"
    r"gradio|huggingface|space|api|无限制|giveaway|launch|new model|release|"
    r"now available|trial|credits",
    re.I,
)

# 明显不是「站点」的噪声
NOISE = re.compile(r"^(RT @|https?://\S+$)", re.I)


def load_source(url=None, path=None):
    if path:
        raw = open(path, encoding="utf-8").read()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "ignore")
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def parse_ts(rec):
    if rec.get("ts"):
        return float(rec["ts"])
    s = rec.get("created_at") or ""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        return 0.0


def guess_capability(text):
    t = text.lower()
    rules = [
        ("视频", r"video|veo|sora|kling|runway|animate"),
        ("音频", r"audio|voice|tts|music|speech|whisper"),
        ("图生图/编辑", r"edit|inpaint|img2img|image editing|remove background|抠图|去背景"),
        ("文生图", r"image|diffusion|flux|midjourney|img|绘图|生图"),
        ("超分/修复", r"upscal|super.?resolution|restore|修复|超分"),
        ("文本/模型", r"llm|chat|model|reasoning|vlm|agent|文本"),
    ]
    for name, pat in rules:
        if re.search(pat, t):
            return name
    return "其他"


def free_hint(text):
    hits = [m.group(0) for m in re.finditer(
        r"free|免费|白嫖|no signup|unlimited|无限制|free tier|open source|开源", text, re.I)]
    return ", ".join(dict.fromkeys(hits))[:60] if hits else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--file")
    ap.add_argument("--since", type=float, default=48, help="最近 N 小时内（0=不限）")
    ap.add_argument("--seen", default="seen_cloud.json")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--max", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.url and not args.file:
        sys.exit("需要 --url 或 --file")

    records = load_source(args.url, args.file)
    now = time.time()

    try:
        seen = set(json.load(open(args.seen, encoding="utf-8")))
    except Exception:
        seen = set()

    cands, skipped = [], 0
    for r in records:
        rid = str(r.get("id") or r.get("url") or "")
        text = r.get("text") or ""
        ts = parse_ts(r)
        if not rid or not text:
            continue
        if rid in seen:
            skipped += 1
            continue
        if args.since and ts and (now - ts) > args.since * 3600:
            continue
        if NOISE.search(text.strip()) or not KEYWORDS.search(text):
            continue
        text = html.unescape(text)
        urls = [u for u in (r.get("urls") or []) if not re.search(r"x\.com|twitter\.com|t\.co", u)]
        # 老格式没有 urls 字段，从正文里捞裸链接
        for m in re.findall(r"https?://[^\s\"'）)】]+", text):
            if re.search(r"x\.com|twitter\.com|t\.co|pic\.", m):
                continue
            if m not in urls:
                urls.append(m)
        cands.append({
            "id": rid,
            "title": text.split("\n")[0][:60].strip(),
            "x_post_author": r.get("author", ""),
            "x_url": r.get("x_url") or r.get("url", ""),
            "site_urls": urls,
            "free_hint": free_hint(text),
            "capability": guess_capability(text),
            "likes": r.get("likes", 0),
            "raw_text": text[:300],
            "created_at": r.get("created_at", ""),
        })

    # 有关键词 + 有外链的排前面
    cands.sort(key=lambda c: (bool(c["site_urls"]), c["likes"] or 0), reverse=True)
    cands = cands[: args.max]

    os.makedirs(args.out_dir, exist_ok=True)
    if not args.dry_run:
        json.dump(cands, open(os.path.join(args.out_dir, "candidates.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        seen |= {c["id"] for c in cands}
        json.dump(sorted(seen), open(args.seen, "w", encoding="utf-8"))

    lines = [f"# 打野情报 digest · {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"源记录 {len(records)} 条，历史已见 {skipped} 条，本次候选 **{len(cands)}** 条。", ""]
    for i, c in enumerate(cands, 1):
        link = c["site_urls"][0] if c["site_urls"] else "（帖内无外链）"
        lines.append(f"{i}. **{c['title']}** — @{c['x_post_author']} · {c['capability']}"
                     f"{' · 免费线索: ' + c['free_hint'] if c['free_hint'] else ''}")
        lines.append(f"   - 站点: {link}")
        lines.append(f"   - 原帖: {c['x_url']}")
    digest = "\n".join(lines)
    if not args.dry_run:
        open(os.path.join(args.out_dir, "digest.md"), "w", encoding="utf-8").write(digest)

    print(digest if not args.dry_run else digest[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
