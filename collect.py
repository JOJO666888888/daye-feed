"""云端侧：读取 feed → 出候选清单（所有 AI 品类 + 额度/优惠 + VPS 算力）

X 抓取只能在能出网的机器上做（Actions），那台机器把 feed.jsonl 推到公开仓后，
这里把增量拉下来：关键词过滤 + 去重 + 品类归类 + 福利类型识别，产出
  candidates.json  给 P2 评分用
  digest.md         人看的简报

用法：
  python collect.py --url https://raw.githubusercontent.com/<user>/daye-feed/main/feed.jsonl \
                    --since 26 --out-dir out
  python collect.py --file feed.jsonl --since 96 --dry-run
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.request

# 命中即认为「可能值得看」的总闸门，宁宽勿窄，后面还有分类和评分
KEYWORDS = re.compile(
    r"free|免费|白嫖|白给|no signup|unlimited|无限制|open.?source|开源|"
    r"playground|beta|waitlist|invite|credits?|quota|额度|trial|试用|限免|"
    r"coupon|promo|折扣|优惠|兑换码|giveaway|launch|release|now available|"
    r"gradio|huggingface|space|api|vps|gpu|compute|hosting|server|算力|服务器|云主机|"
    r"tts|voice|music|video|image|model",
    re.I,
)

# 明显不是「站点」的噪声
NOISE = re.compile(r"^(RT @|https?://\S+$)", re.I)

# 垃圾/推广帖
SPAM = re.compile(
    r"18\+|nsfw|porn|xxx|onlyfans|adult content|\bsex\b|airdrop|"
    r"casino|betting|forex|follow (me|back)|dm (me|for)",
    re.I,
)

# 品类：顺序即优先级，取第一个命中
CAPS = [
    ("视频", r"\bvideo|veo|sora|kling|runway|pika|animate|i2v|t2v|视频|影视"),
    ("音频/语音", r"\baudio|\bvoice|\btts\b|speech|music|suno|elevenlabs|配音|语音|音乐"),
    ("图像", r"\bimage|diffusion|flux|midjourney|nano.?banana|seedream|img2img|"
             r"inpaint|upscal|生图|绘图|抠图|修图|出图|超分"),
    ("代码/IDE", r"\bcod(e|ing)\b|cursor|copilot|\bide\b|代码|编程"),
    ("Agent/工具", r"\bagent|workflow|automation|\bmcp\b|智能体|工作流"),
    ("算力/VPS", r"\bvps\b|\bgpu\b|compute|hosting|cloud|server|colocation|"
                 r"算力|服务器|云主机|主机|杜甫"),
    ("文本/LLM", r"\bllm\b|\bchat\b|reasoning|\bvlm\b|\bmodel\b|token|大模型|文本|推理"),
]

# 福利类型，可多选
PERKS = [
    ("免费额度", r"free (credits?|tier|quota|tokens?)|credits?\b|额度|赠送|白给"),
    ("免登录", r"no signup|without (an )?account|免登录|无需注册|不用注册"),
    ("无限制", r"unlimited|无限制|不限量"),
    ("限时免费/试用", r"free trial|\btrial\b|试用|限免|beta|waitlist|公测"),
    ("优惠/兑换", r"discount|coupon|promo(?:tion)? code|% ?off|优惠|折扣|兑换码|代金券"),
    ("开源", r"open.?source|开源|apache|mit licen[sc]e"),
]


def load_source(url=None, path=None, retries=3, timeout=30):
    if path:
        raw = open(path, encoding="utf-8").read()
    else:
        raw, last = None, None
        for i in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read().decode("utf-8", "ignore")
                break
            except Exception as e:
                last = e
                print(f"[!] 拉取失败({i + 1}/{retries})：{type(e).__name__}", file=sys.stderr)
                time.sleep(3 * (i + 1))
        if raw is None:
            raise SystemExit(f"拉取失败：{last}")
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def parse_ts(rec):
    if rec.get("ts"):
        return float(rec["ts"])
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(rec.get("created_at") or "").timestamp()
    except Exception:
        return 0.0


def classify(text):
    for name, pat in CAPS:
        if re.search(pat, text, re.I):
            return name
    return "其他"


def perks(text):
    return [name for name, pat in PERKS if re.search(pat, text, re.I)]


def pick_urls(rec, text):
    urls = []
    for u in rec.get("urls") or []:
        if re.search(r"x\.com|twitter\.com|t\.co/|pic\.", u):
            continue
        if u not in urls:
            urls.append(u)
    for m in re.findall(r"https?://[^\s\"'）)】]+", text):
        if re.search(r"x\.com|twitter\.com|t\.co/|pic\.", m):
            continue
        if m not in urls:
            urls.append(m)
    return urls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--file")
    ap.add_argument("--since", type=float, default=26, help="最近 N 小时（0=不限）")
    ap.add_argument("--seen", default="seen_cloud.json")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--max", type=int, default=80)
    ap.add_argument("--min-likes", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.url and not args.file:
        sys.exit("需要 --url 或 --file")

    records = load_source(args.url, args.file, timeout=args.timeout)
    now = time.time()
    try:
        seen = set(json.load(open(args.seen, encoding="utf-8")))
    except Exception:
        seen = set()

    cands, skipped = [], 0
    for r in records:
        rid = str(r.get("id") or r.get("url") or "")
        text = html.unescape(r.get("text") or "")
        ts = parse_ts(r)
        if not rid or not text:
            continue
        if rid in seen:
            skipped += 1
            continue
        if args.since and ts and (now - ts) > args.since * 3600:
            continue
        if NOISE.search(text.strip()) or SPAM.search(text) or not KEYWORDS.search(text):
            continue
        if args.min_likes and (r.get("likes") or 0) < args.min_likes:
            continue
        cands.append({
            "id": rid,
            "title": text.split("\n")[0][:70].strip(),
            "x_post_author": r.get("author", ""),
            "x_url": r.get("x_url") or r.get("url", ""),
            "site_urls": pick_urls(r, text),
            "category": classify(text),
            "perks": perks(text),
            "likes": r.get("likes", 0),
            "raw_text": text[:300],
            "created_at": r.get("created_at", ""),
        })

    cands.sort(key=lambda c: (bool(c["site_urls"]), c["likes"] or 0), reverse=True)
    cands = cands[: args.max]

    os.makedirs(args.out_dir, exist_ok=True)
    if not args.dry_run:
        json.dump(cands, open(os.path.join(args.out_dir, "candidates.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        seen |= {c["id"] for c in cands}
        json.dump(sorted(seen), open(args.seen, "w", encoding="utf-8"))

    by_cat = {}
    for c in cands:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    stat = " · ".join(f"{k} {v}" for k, v in sorted(by_cat.items(), key=lambda x: -x[1]))

    lines = [f"# 打野情报 digest · {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"源记录 {len(records)} 条，历史已见 {skipped} 条，本次候选 **{len(cands)}** 条。", "",
             f"品类分布：{stat or '无'}", ""]
    for i, c in enumerate(cands, 1):
        link = c["site_urls"][0] if c["site_urls"] else "（帖内无外链）"
        tag = " · ".join(c["perks"]) if c["perks"] else "福利未标注"
        lines.append(f"{i}. **{c['title']}** — @{c['x_post_author']} · {c['category']} · {tag}")
        lines.append(f"   - 站点：{link}")
        if len(c["site_urls"]) > 1:
            lines.append(f"   - 其它链接：{', '.join(c['site_urls'][1:4])}")
        lines.append(f"   - 原帖：{c['x_url']}")
    digest = "\n".join(lines)
    if not args.dry_run:
        open(os.path.join(args.out_dir, "digest.md"), "w", encoding="utf-8").write(digest)
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
