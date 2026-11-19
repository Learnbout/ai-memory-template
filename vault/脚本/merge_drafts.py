# -*- coding: utf-8 -*-
"""草稿归并：把 待记草案.jsonl 里堆积的自动草稿，按主题合并成可检索的库内条目。

设计要点（为什么这么做）：
1. **输入用草稿、不用 transcript**：实测 transcript 的 user 消息大半是系统脚手架，
   噪声太大；草稿里的 user_query_snippet 是中位 69 字符的干净问句。
2. **零 LLM**：聚类用现成的 bge-small-zh 向量 + 贪心最近邻；标题词用 IDF 加权的 2-gram 现算。
   理由：170 个以上条目逐条调模型既贵又不稳定，且本机 CPU。
3. **不动原始文件**：处理进度写独立 ledger（append-only），原 待记草案.jsonl 是 hook 的产出，只读。
4. **必须留淘汰位**：每条写入 verified=False + 记来源条数，后续淘汰作业按「帮过/误导」计数裁剪。

用法：
  python merge_drafts.py --dry-run     # 只报告聚类结果，不写库
  python merge_drafts.py --write       # 真正写入（会走 memory_write）
"""
from __future__ import annotations
from pathlib import Path

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime

import numpy as np

VAULT = os.environ.get('AI_MEMORY_DIR') or str(Path(__file__).resolve().parents[1])
WB = os.path.join(VAULT, '.workbuddy')
DRAFTS = os.path.join(WB, '待记草案.jsonl')
LEDGER = os.path.join(WB, '草稿归并_ledger.jsonl')

# 本脚本要 import 库根的 embed_onnx / memory_tools（它们不在 脚本/ 下）
if VAULT not in sys.path:
    sys.path.insert(0, VAULT)
os.environ.setdefault('AI_MEMORY_DIR', VAULT)

# 明显噪声：这些片段不是"用户提的问题"
NOISE_MARKERS = (
    'Please continue with the conversation',
    'This session is being continued',
    'Network error occurred',
    'Caveat: The messages below were generated',
    '<system-reminder',
    'CURRENT_DATE',
)
# 2026-11-18 试跑后补：草稿里的 user_query_snippet 有大量非用户内容 ——
# 工具回执（call_xxx completed / failed）、shell 提示符（PS C:\Users\...）、
# 系统指令（You MUST / Do not create ... / follow these steps）、以及被截断的路径。
# 这一层过滤决定「归并出来的是人话还是垃圾」，所以宁严不宽。
TOOL_NOISE_MARKERS = (
    'call_', 'completed', 'failed', 'Copyright (C)', 'PS C:\\', 'powershell',
    'ExecutionPolicy', 'You MUST', 'Do not create', 'follow these steps',
    'Caveat:', 'tool_call', '<function', 'stdout', 'stderr', 'exit code',
    # 2026-11-18 二轮补：中文报错文本的中文占比很高，会被上面那条「必须含中文」放过，
    # 但它仍然是命令输出，不是用户问的问题。
    '不是内部或外部命令', '系统找不到指定的路径', '命中不了', '命令语法不正确',
    'Traceback', 'ModuleNotFoundError', 'PermissionError', 'FileNotFoundError',
    'SyntaxError', 'Error:', 'ERROR', '拒绝访问', '操作超时',
)
SHELL_PROMPT_RE = re.compile(r'^(PS [A-Z]:\\|C:\\Users\\|>|[a-z]:\\)', re.I)
CJK_RE = re.compile(r'[\u4e00-\u9fff]')


def cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    cjk = len(CJK_RE.findall(s))
    visible = len(re.sub(r'\s', '', s))
    return cjk / max(1, visible)


def looks_like_user_question(s: str) -> bool:
    """判据：像人说的话，而不是工具输出/系统指令。

    - 必须含中文（这条库里用户都用中文提问；纯英文片段基本都是脚手架）
    - 中文占比 ≥ 40%
    - 长度 ≥ 12
    - 不含工具回执/系统指令特征串
    """
    if len(s) < 12:
        return False
    if any(m in s for m in TOOL_NOISE_MARKERS):
        return False
    low = s.lower()
    if low.startswith('ps ') or low.startswith('pwsh'):
        return False
    if SHELL_PROMPT_RE.match(s):
        return False
    if cjk_ratio(s) < 0.40:
        return False
    return True

COS_THRESHOLD = 0.82      # 归到同一主题的最小余弦
MIN_CLUSTER = 2           # 少于 N 条的簇不单独成篇（并进月度的「零散」）
MAX_NOTES = 120           # 单次最多产出多少篇，防爆炸


def load_drafts() -> list[dict]:
    if not os.path.isfile(DRAFTS):
        return []
    recs = []
    with open(DRAFTS, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            recs.append(r)
    return recs


def load_ledger() -> set[str]:
    done = set()
    if not os.path.isfile(LEDGER):
        return done
    with open(LEDGER, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)['key'])
            except Exception:
                continue
    return done


def draft_key(r: dict) -> str:
    raw = '%s|%s|%s' % (r.get('ts'), r.get('session_id'), (r.get('user_query_snippet') or '')[:60])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def clean_snippet(s: str) -> str:
    s = re.sub(r'<[^>]{1,80}>', ' ', s or '')
    s = re.sub(r'@long-text:\s*"[^"]{0,200}"', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def is_noise(s: str) -> bool:
    return any(m in s for m in NOISE_MARKERS) or len(s) < 8


def bigrams(s: str) -> set[str]:
    s = ''.join(ch for ch in s if not ch.isspace()).lower()
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    words = {w for w in re.findall(r'[a-z][a-z0-9_\-]{2,}', s)}
    return grams | words


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--limit', type=int, default=0, help='只处理最近 N 条（0=全部）')
    args = ap.parse_args()

    drafts = load_drafts()
    done = load_ledger()
    fresh = [d for d in drafts if draft_key(d) not in done]
    if args.limit:
        fresh = fresh[-args.limit:]
    items = []
    rejected = Counter()
    for d in fresh:
        s = clean_snippet(d.get('user_query_snippet') or '')
        if is_noise(s):
            rejected['太短/空'] += 1
            continue
        if not looks_like_user_question(s):
            rejected['工具输出或系统指令'] += 1
            continue
        items.append({
            'key': draft_key(d), 'ts': d.get('ts', ''), 'cwd': d.get('cwd', ''),
            'snip': s, 'tools': d.get('substantive_tools') or [],
        })

    print('草稿总数 %d，已处理 %d，本次待处理 %d，清洗后有效 %d' % (
        len(drafts), len(done), len(fresh), len(items)))
    if rejected:
        print('  被过滤: %s' % dict(rejected))
    if not items:
        print('没有可处理的新草稿')
        return 0

    import embed_onnx
    texts = [it['snip'][:180] for it in items]
    vecs = np.asarray(embed_onnx.encode(texts), dtype=np.float32)
    vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)

    # 贪心聚类：与已有簇质心比较
    clusters: list[dict] = []
    for i, v in enumerate(vecs):
        best, best_sim = None, -1.0
        for c in clusters:
            sim = float(np.dot(v, c['centroid']))
            if sim > best_sim:
                best_sim, best = sim, c
        if best is not None and best_sim >= COS_THRESHOLD:
            best['idx'].append(i)
            best['centroid'] = (best['centroid'] * (len(best['idx']) - 1) + v) / len(best['idx'])
        else:
            clusters.append({'idx': [i], 'centroid': v.copy()})

    clusters.sort(key=lambda c: -len(c['idx']))
    multi = [c for c in clusters if len(c['idx']) >= MIN_CLUSTER]
    single = [c for c in clusters if len(c['idx']) < MIN_CLUSTER]
    print('聚类：%d 簇（≥%d 条的 %d 簇，单条 %d 簇）' % (
        len(clusters), MIN_CLUSTER, len(multi), len(single)))

    # IDF（在草稿语料上算，用于出标题词）
    df = Counter()
    grams_of = [bigrams(it['snip']) for it in items]
    for g in grams_of:
        df.update(g)
    N = len(items)
    idf = {g: math.log((N + 1) / (c + 1)) + 1.0 for g, c in df.items()}

    def title_of(idx: list[int], tag: str = '') -> str:
        cnt = Counter()
        for i in idx:
            for g in grams_of[i]:
                # 标题只取中文 2-gram：英文片段在草稿里几乎都是工具名/路径残渣
                if len(g) == 2 and CJK_RE.search(g):
                    cnt[g] += 1
        if not cnt:
            return '草稿归并_%s' % (tag or '未命名')
        scored = sorted(cnt.items(), key=lambda kv: -(kv[1] * idf.get(kv[0], 1.0)))
        picked: list[str] = []
        for g, _ in scored:
            if len(g) < 2:
                continue
            if any(g in p or p in g for p in picked):
                continue
            picked.append(g)
            if len(picked) >= 3:
                break
        return '草稿归并_' + ''.join(picked)

    notes = []
    for c in multi:
        idx = c['idx']
        ts = sorted(items[i]['ts'] for i in idx if items[i]['ts'])
        span = '%s~%s' % (ts[0][:10], ts[-1][:10]) if ts else ''
        body_lines = ['## 来源', '',
                      '- 由 `待记草案.jsonl` 自动归并：%d 条草稿，时间跨度 %s' % (len(idx), span),
                      '- 生成方式：bge-small-zh 向量贪心聚类（余弦 ≥ %.2f）+ IDF 标题词，零 LLM 调用' % COS_THRESHOLD,
                      '- 状态：**未验证**。这是一条「以前会漏记」的线索索引，细节需回 transcript 或重做。',
                      '', '## 草稿清单', '']
        for i in idx:
            it = items[i]
            tools = ','.join(sorted(set(it['tools'])))[:60]
            # 2026-11-18：**不截断**。实测把草稿截到 200 字符存，用户用完整问句回查时
            # 子串匹配直接失败（召回只有 30%）。草稿本来就短（中位 69 字符），全存不占地方。
            body_lines.append('- `%s` %s%s' % (
                it['ts'][:16], it['snip'], (' ｜工具: ' + tools) if tools else ''))
        notes.append({'title': title_of(idx), 'body': '\n'.join(body_lines), 'keys': [items[i]['key'] for i in idx]})

    # 单条簇按天聚成「流水草稿」。
    # 2026-11-18 试跑后改：原本按「月」聚，252 条单例挤进 2 篇，等于一个没人会读的大清单；
    # 按天聚则每篇只装当天那几条，标题带日期，检索时按内容命中即可。**不硬造主题**。
    if single:
        by_day: dict[str, list[int]] = {}
        for c in single:
            i = c['idx'][0]
            day = (items[i]['ts'] or '')[:10] or '未知'
            by_day.setdefault(day, []).append(i)
        for day, idx in sorted(by_day.items()):
            # 2026-11-18 三轮改：标题里带上当天的关键词短语。
            # 起因：只用「流水草稿_日期」时，用户拿当初那句长话回查，正文确实命中（实测 40/40），
            # 但长查询会命中两三百篇、按更新时间排就把这条埋了（top-5 只有 30%）。
            # 加上关键词后它有机会走「标题命中优先」那一档。
            kw = title_of(idx).replace('草稿归并_', '')
            body_lines = ['## 来源', '',
                          '- 由 `待记草案.jsonl` 自动归并的**流水草稿**（%s，%d 条，彼此不相似）' % (day, len(idx)),
                          '- 为什么按天成篇而不是硬聚类：这些草稿是一次性的，强行聚类只会造出假主题。',
                          '- 用途：它们原本会彻底漏记；现在至少能被关键词/向量检索命中，细节回 transcript 或原始会话。',
                          '- 状态：**未验证**。', '', '## 清单', '']
            for i in idx:
                it = items[i]
                body_lines.append('- `%s` %s' % (it['ts'][:16], it['snip']))
            notes.append({'title': '流水草稿_%s_%s' % (day, kw), 'body': '\n'.join(body_lines),
                          'keys': [items[i]['key'] for i in idx]})

    notes = notes[:MAX_NOTES]
    print('拟产出 %d 篇（上限 %d）' % (len(notes), MAX_NOTES))
    print('')
    for n in notes[:8]:
        print('  [%2d 条] %s' % (len(n['keys']), n['title']))
        first = [ln for ln in n['body'].splitlines() if ln.startswith('- `')][:2]
        for ln in first:
            print('        ' + ln[:120])
    if len(notes) > 8:
        print('  …另 %d 篇' % (len(notes) - 8))

    total_keys = sum(len(n['keys']) for n in notes)
    print('')
    print('覆盖草稿 %d 条（占有效 %d 的 %.0f%%）' % (total_keys, len(items), total_keys / len(items) * 100))

    if not args.write:
        print('')
        print('[dry-run] 未写入。确认无误后加 --write')
        return 0

    # 写入走 memory_write（库的唯一写入口）
    sys.path.insert(0, VAULT)
    os.environ.setdefault('AI_MEMORY_DIR', VAULT)
    import memory_tools as mt
    written = 0
    failed = []
    for n in notes:
        try:
            # allow_duplicate=True 是必须的：本脚本会一次产出「流水草稿_2026-10-10 /
            # _10-11 / …」这种同前缀家族，memory_write 的重名护栏会把它们判成
            # 「同主题已有条目」而拒绝新建（2026-11-18 实测：32 篇只落地 10 篇）。
            r = mt.memory_write(
                title=n['title'], content=n['body'], tags=[],
                source='草稿归并(%s)' % datetime.now().strftime('%Y-%m-%d'),
                verified=False, allow_duplicate=True,
            )
            # 成功判据必须白名单：护栏拒绝时返回的是「检测到同主题已有条目，本次未写入」，
            # 里面既没有「错误」也没有「失败」—— 用黑名单判会把它当成成功（真实踩过）。
            if isinstance(r, str) and ('已创建记忆' in r or '已更新记忆' in r):
                written += 1
            else:
                failed.append((n['title'], str(r)[:80].replace('\n', ' ')))
        except Exception as exc:  # noqa: BLE001
            failed.append((n['title'], repr(exc)[:80]))
    if failed:
        print('  失败 %d 篇：' % len(failed))
        for t, why in failed[:6]:
            print('    %s -> %s' % (t, why))
    with open(LEDGER, 'a', encoding='utf-8') as f:
        for n in notes:
            if n['title'] in {t for t, _ in failed}:
                continue  # 没写进去的不许标已处理，否则草稿永远丢了
            for k in n['keys']:
                f.write(json.dumps({'key': k, 'note': n['title'],
                                    'at': datetime.now().isoformat(timespec='seconds')},
                                   ensure_ascii=False) + '\n')
    print('')
    print('已写入 %d/%d 篇；ledger 追加 %d 条' % (written, len(notes),
                                              sum(len(n['keys']) for n in notes if n['title'] not in {t for t, _ in failed})))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
