# daye-feed

打野情报的**只读发布端**：X 时间线抓取的原始数据 + 云端提取脚本。写入由私有仓
`daye-x-intel` 的 Actions 通过部署密钥完成，这里不跑任何抓取。

| 文件 | 内容 |
|---|---|
| `feed.jsonl` | 抓取产物，一行一条推文：`id / author / text / urls / created_at / ts / likes / x_url` |
| `collect.py` | 把 feed 变成候选表（关键词过滤、去重、能力分类），输出 `candidates.json` + `digest.md` |

## 云端消费（无需任何凭据）

```bash
curl -sO https://raw.githubusercontent.com/JOJO666888888/daye-feed/main/collect.py
curl -sO https://raw.githubusercontent.com/JOJO666888888/daye-feed/main/feed.jsonl
python collect.py --file feed.jsonl --since 24 --seen seen.json --out-dir out
```

公开数据的边界：这里只有公开推文的文本和链接，没有 cookie、没有账号信息。
