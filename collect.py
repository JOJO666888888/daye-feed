"""云端侧：读取 feed → 出候选清单（全品类零成本资源）

定位变了：不再只盯 AI 和 VPS，改成「任何不花钱、不绑卡就能拿到的东西」——
AI 额度、云主机试用、软件限免/正版授权、设计素材、课程、域名 SSL、游戏喜加一、
生活类优惠券。硬约束两条：**不绑卡、不实际付费**。

X 抓取只能在能出网的机器上做（Actions），那台机器把 feed.jsonl 推到公开仓后，
这里把增量拉下来：过滤 + 去重 + 品类归类 + 福利类型 + 绑卡/付费判定，产出
  candidates.json  给评分用
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

# ── 判定：必须同时命中「福利词」和「主题词」 ──────────────────────────
BENEFIT = re.compile(
    r"\bfree\b|freebie|免费|白嫖|白给|零成本|no cost|"
    r"giveaway|限免|永久免费|终身|"
    r"credits?|quota|额度|赠金|赠送|试用|trial|beta|waitlist|公测|"
    r"coupon|promo|discount|% ?off|折扣|优惠|兑换码|代金券|满减|"
    r"open.?source|开源|apache|mit licen[sc]e|"
    r"student (pack|plan)|学生包|教育优惠|lifetime deal|限时|"
    r"1 year free|首年免费|免费领|领取|白拿",
    re.I,
)

TOPIC = re.compile(
    # AI 各品类
    r"\bai\b|\bllm\b|gpt|claude|gemini|deepseek|qwen|kimi|模型|大模型|推理|"
    r"image|diffusion|flux|midjourney|seedream|生图|绘图|出图|抠图|修图|"
    r"video|veo|sora|kling|runway|pika|视频生成|"
    r"audio|voice|\btts\b|speech|music|suno|语音|配音|音乐|"
    r"\bcoding\b|cursor|copilot|\bide\b|代码|编程|程序员|"
    r"agent|\bmcp\b|智能体|workflow|工作流|"
    # 云与主机
    r"\bvps\b|\bgpu\b|compute|hosting|cloud|server|colocation|domain|"
    r"\bssl\b|\bdns\b|mail(box)?|云主机|主机|服务器|算力|域名|证书|邮箱|"
    # 软件与授权
    r"licen[sc]e|activation|激活|正版|office|windows|adobe|jetbrains|"
    r"subscription|会员|\bsaas\b|软件|插件|extension|app\b|"
    # 设计素材
    r"font|字体|icon|图标|template|模板|mockup|stock (photo|image)|图库|"
    r"illustration|插画|preset|调色|素材|"
    # 学习
    r"course|课程|教程|tutorial|ebook|电子书|bootcamp|认证|certification|"
    r"udemy|coursera|学习|训练营|"
    # 游戏娱乐
    r"steam|epic|gog|\bgame\b|游戏|dlc|喜加一|"
    # 开发者
    r"\bapi\b|\bsdk\b|database|数据库|ci/cd|monitoring|监控|"
    # 电商生活
    r"优惠券|折扣码|满减|包邮|会员卡|流量|话费|"
    # 兜底：常见的产品/服务词
    r"huggingface|gradio|playground|token|模型服务|工具|平台|服务",
    re.I,
)

# 明显不是「站点」的噪声
NOISE = re.compile(r"^(RT @|https?://\S+$)", re.I)

# 垃圾/推广帖
SPAM = re.compile(
    r"18\+|nsfw|porn|xxx|onlyfans|adult content|\bsex\b|airdrop|"
    r"casino|betting|forex|follow (me|back)|dm (me|for)|"
    r"pxf\.io|bit\.ly|tinyurl|shorturl|linktr\.ee|t\.me/\+|"
    r"betano|1xbet|betwinner|melbet|\biphone\b|\bprize\b|claim your (prize|reward)|"
    r"招代理|招商|代理加盟|刷单|兼职|日赚|躺赚|加微信",
    re.I,
)

# 品类：顺序即优先级，取第一个命中
CAPS = [
    ("AI·视频", r"\bvideo|veo|sora|kling|runway|pika|animate|i2v|t2v|视频|影视|出片"),
    ("AI·音频", r"\baudio|\bvoice|\btts\b|speech|music|suno|elevenlabs|配音|语音|音乐"),
    ("AI·图像", r"\bimage|diffusion|flux|midjourney|nano.?banana|seedream|img2img|"
                r"inpaint|upscal|生图|绘图|抠图|修图|出图|超分"),
    ("AI·代码", r"\bcoding\b|\bcode (agent|editor|completion|model|assistant)|"
                r"cursor|copilot|\bide\b|代码|编程|程序员"),
    ("AI·Agent", r"\bagent|workflow|automation|\bmcp\b|智能体|工作流"),
    ("AI·文本", r"\bllm\b|\bchat\b|reasoning|\bvlm\b|\bmodel\b|token|大模型|文本|推理"),
    ("云与主机", r"\bvps\b|\bgpu\b|compute|hosting|cloud|server|colocation|"
                 r"domain|\bssl\b|\bdns\b|mailbox|算力|服务器|云主机|主机|域名|证书|邮箱"),
    ("软件授权", r"licen[sc]e|activation|激活|正版|office|windows|adobe|jetbrains|"
                 r"subscription|会员|\bsaas\b|软件|插件|extension"),
    ("设计素材", r"font|字体|icon|图标|template|模板|mockup|stock (photo|image)|"
                 r"图库|illustration|插画|preset|素材"),
    ("学习资源", r"course|课程|教程|tutorial|ebook|电子书|bootcamp|认证|"
                 r"certification|udemy|coursera|学习|训练营"),
    ("开发者工具", r"\bapi\b|\bsdk\b|database|数据库|ci/cd|monitoring|监控|github|"
                   r"supabase|vercel|railway|cloudflare"),
    ("游戏娱乐", r"steam|epic|gog|\bgame\b|游戏|dlc|喜加一"),
    ("电商生活", r"优惠券|折扣码|满减|包邮|会员卡|流量|话费|外卖|打车"),
]

# 福利类型，可多选
PERKS = [
    ("免费额度", r"free (credits?|tier|quota|tokens?)|credits?\b|额度|赠金|赠送|白给|白拿"),
    ("免登录", r"no signup|without (an )?account|免登录|无需注册|不用注册|no account"),
    ("无限制", r"unlimited|无限制|不限量|无限"),
    ("限时免费/试用", r"free trial|\btrial\b|试用|限免|beta|waitlist|公测|限时|截止"),
    ("优惠/兑换", r"discount|coupon|promo(?:tion)? code|% ?off|优惠|折扣|兑换码|代金券|满减"),
    ("开源", r"open.?source|开源|apache|mit licen[sc]e"),
    ("正版授权", r"licen[sc]e key|activation key|正版|激活码|giveaway"),
    ("学生/教育", r"student (pack|plan)|学生包|教育优惠|edu\b"),
    ("永久", r"lifetime|permanent|永久|终身"),
]

# 明确要绑卡 / 明确不要卡
CARD_REQUIRED = re.compile(
    r"(credit|debit) card (is )?required|requires? a (credit|debit) card|"
    r"payment method (is )?required|add a payment method|billing (info|information) required|"
    r"需(要)?(绑定|绑)?(信用卡|银行卡|支付方式)|必须绑卡|需实名认证|"
    r"card on file|without a credit card you can'?t",
    re.I,
)
CARD_FREE = re.compile(
    r"no (credit|debit) card( required)?|without a (credit|debit) card|"
    r"no card( needed| required)?|"
    r"无需(绑定)?(信用卡|银行卡)|不用绑卡|不需要信用卡|无需支付方式",
    re.I,
)

# 明确要付费
PAY_REQUIRED = re.compile(
    r"starting (at|from) \$|per (month|user|seat)|/mo\b|/month|"
    r"paid plan|upgrade to (pro|premium|plus)|subscription (fee|cost)|"
    r"minimum spend|deposit required|充值|付费|订阅费|"
    r"\$\s?\d+(\.\d+)?\s*(/|per)?\s*(mo|month|year|yr|月|年)",
    re.I,
)

# 截止时间
DEADLINE = re.compile(
    r"ends? (on|at)?\s*\w+|expires?|deadline|until \w+|"
    r"截止|截至|活动时间|限时到|有效期至",
    re.I,
)


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


def card_state(text):
    """no / required / unknown"""
    if CARD_FREE.search(text):
        return "no"
    if CARD_REQUIRED.search(text):
        return "required"
    return "unknown"


def pay_state(text):
    """True=明确要付费；False=明确免费；None=不确定"""
    if CARD_FREE.search(text) or re.search(r"\bfree\b|免费|白嫖|0 元|zero cost", text, re.I):
        # 有 free 又有价格，可能是「免费版 vs 付费版」，仍按 unknown 处理
        if PAY_REQUIRED.search(text):
            return None
        return False
    if PAY_REQUIRED.search(text):
        return True
    return None


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
    ap.add_argument("--keep-paid", action="store_true",
                    help="保留明确要付费的条目（默认丢弃，只留零成本）")
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

    cands, skipped, paid_dropped, card_dropped = [], 0, 0, 0
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
        if NOISE.search(text.strip()) or SPAM.search(text):
            continue
        if not (BENEFIT.search(text) and TOPIC.search(text)):
            continue
        if args.min_likes and (r.get("likes") or 0) < args.min_likes:
            continue

        card = card_state(text)
        pay = pay_state(text)
        if not args.keep_paid and pay is True:
            paid_dropped += 1
            continue
        if card == "required":
            card_dropped += 1
            if not args.keep_paid:
                continue

        cands.append({
            "id": rid,
            "title": text.split("\n")[0][:70].strip(),
            "x_post_author": r.get("author", ""),
            "x_url": r.get("x_url") or r.get("url", ""),
            "site_urls": pick_urls(r, text),
            "category": classify(text),
            "perks": perks(text),
            "card_required": card,
            "pay_required": pay,
            "deadline_hint": (DEADLINE.search(text).group(0)[:40] if DEADLINE.search(text) else ""),
            "likes": r.get("likes", 0),
            "raw_text": text[:400],
            "created_at": r.get("created_at", ""),
        })

    # 同一条内容被多个搬运号刷屏 → 按「标题归一 + 主域名」去重，留互动最高的
    dedup, keys = [], set()
    for c in sorted(cands, key=lambda c: c["likes"] or 0, reverse=True):
        dom = ""
        if c["site_urls"]:
            dom = re.sub(r"^https?://(www\.)?", "", c["site_urls"][0]).split("/")[0].lower()
        key = (re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", c["title"].lower())[:40], dom)
        if key in keys:
            continue
        keys.add(key)
        dedup.append(c)
    cands = dedup

    # 排序：不要卡 > 未知 > 要卡；有外链优先；福利条数多优先
    card_rank = {"no": 0, "unknown": 1, "required": 2}
    cands.sort(key=lambda c: (card_rank.get(c["card_required"], 3),
                              c["pay_required"] is True,
                              not c["site_urls"],
                              -len(c["perks"]),
                              -(c["likes"] or 0)))
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

    lines = [f"# 打野 digest · {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"源记录 {len(records)} 条｜历史已见 {skipped}｜本次候选 **{len(cands)}**｜"
             f"丢弃：要付费 {paid_dropped}、要绑卡 {card_dropped}", "",
             f"品类：{stat or '无'}", ""]

    groups = {}
    for c in cands:
        groups.setdefault(c["category"], []).append(c)
    for cat, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        lines.append(f"## {cat}（{len(items)}）")
        for c in items:
            link = c["site_urls"][0] if c["site_urls"] else "（帖内无外链）"
            tag = " · ".join(c["perks"]) if c["perks"] else "福利未标注"
            card = {"no": "免绑卡", "required": "要绑卡", "unknown": "绑卡未知"}[c["card_required"]]
            lines.append(f"- **{c['title']}** — @{c['x_post_author']} · {tag} · {card}")
            lines.append(f"  - 站点：{link}")
            if c["deadline_hint"]:
                lines.append(f"  - 时限：{c['deadline_hint']}")
            lines.append(f"  - 原帖：{c['x_url']}")
        lines.append("")

    digest = "\n".join(lines)
    if not args.dry_run:
        open(os.path.join(args.out_dir, "digest.md"), "w", encoding="utf-8").write(digest)
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
