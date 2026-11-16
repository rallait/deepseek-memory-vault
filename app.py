"""DeepShare · DeepSeek 分享抓取记忆库
------------------------------------------------------------
一个纯本地运行的 Streamlit 应用：
1. 侧边栏输入 DeepSeek 分享链接 -> Playwright 抓取 -> 清洗为 Markdown -> 落盘 my_memories/
2. 主界面倒序展示记忆、关键词搜索、Markdown/代码高亮渲染

抓取采用 "阶梯式降级" 架构（任何一层成功即返回，不会因单点失败而崩溃）：
    第 1 层   DOM 选择器多路探测（.ds-markdown / [class*="message"] ...）
    第 1.5 层 页面内嵌状态数据（Next.js __NEXT_DATA__ / self.__next_f / SSR JSON）
    第 2 层   Network 监听降级（page.on("response") 抓 JSON 接口）
    第 3 层   重试与兜底（伪造请求头 / 主站预热拿 Cookie / 滚动懒加载 / 刷新重试 / 有头浏览器）

运行：
    streamlit run app.py
"""

from __future__ import annotations

import html
import io
import json
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import streamlit as st

# ----------------------------------------------------------------------------
# 常量 & 目录
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
MEMORY_DIR = BASE_DIR / "my_memories"
DEBUG_DIR = BASE_DIR / "debug_artifacts"

# AI 总结配置（本地文件，已在 .gitignore 中排除，不会上传）
AI_CONFIG_PATH = BASE_DIR / ".deepshare_ai.json"
# 本地偏好（当前主题等），同样不上传
PREFS_PATH = BASE_DIR / ".deepshare_prefs.json"
AI_DEFAULTS = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
}
# 单次送入模型的最大字符数（头 + 尾拼接，避免超长对话爆上下文/拖慢响应）
AI_INPUT_HEAD = 12000
AI_INPUT_TAIL = 4000

LogFn = Callable[[str], None]

# 防检测：使用较新的桌面 Chrome UA
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/159.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/159.0.0.0 Mobile Safari/537.36"
)

# 隐藏 webdriver 等自动化特征
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );
}
"""

# 选择器分组：从"最像消息容器"到"整页兜底"，逐组尝试
DOM_SELECTOR_GROUPS: list[list[str]] = [
    [".ds-message", "[class*='ds-message']", ".ds-assistant-message-main-content", "[class*='assistant-message']"],
    ["[class*='message']", "[class*='Message']"],
    ["[data-testid*='message']", "[data-testid*='chat']"],
    ["[class*='chat-item']", "[class*='chatItem']", "[class*='dialog']", "[class*='conversation']"],
    [".ds-markdown", "[class*='ds-markdown']"],
    ["main", "#root"],
]

# 页面上属于"界面噪音"而非对话内容的短文本
NOISE_TEXTS = {
    "复制", "分享", "登录", "注册", "新对话", "下载", "下载 App", "深度思考", "联网搜索",
    "开启新对话", "内容由 AI 生成", "回到顶部", "展开", "收起", "编辑", "重试",
}

# ----------------------------------------------------------------------------
# 页面内执行：把 DOM 转成 Markdown 风格文本块
# ----------------------------------------------------------------------------
EXTRACT_JS = r"""
(selectors) => {
  const out = { title: '', blocks: [], pageText: '', url: location.href, selector: selectors.join(',') };
  out.title = (document.title || '').trim();

  const CHROME_TAGS = ['HEADER', 'NAV', 'ASIDE', 'FOOTER'];
  const JS_CHROME = (el) => {
    let n = el;
    while (n && n !== document.body) {
      if (CHROME_TAGS.indexOf(n.tagName) >= 0) return true;
      const role = n.getAttribute && n.getAttribute('role');
      if (role === 'navigation' || role === 'banner' || role === 'contentinfo' || role === 'complementary') return true;
      if (n.getAttribute && n.getAttribute('aria-hidden') === 'true') return true;
      n = n.parentElement;
    }
    return false;
  };

  // 逐节点序列化成 Markdown（保留代码块、标题、列表、表格）
  const toMd = (root) => {
    const SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, BUTTON: 1, SVG: 1, IMG: 1, TEXTAREA: 1, INPUT: 1 };
    const buf = [];
    const walk = (node) => {
      if (node.nodeType === 3) { buf.push(node.nodeValue); return; }
      if (node.nodeType !== 1) return;
      const tag = node.tagName;
      if (SKIP[tag]) return;
      if (node.getAttribute && node.getAttribute('aria-hidden') === 'true') return;
      if (tag === 'BR') { buf.push('\n'); return; }
      if (tag === 'PRE') {
        const codeEl = node.querySelector('code') || node;
        let raw = codeEl.innerText || codeEl.textContent || '';
        raw = raw.replace(/\s+$/, '');
        let lang = '';
        const cls = (codeEl.getAttribute && codeEl.getAttribute('class')) || '';
        const m = cls.match(/language-([\w+#.-]+)/);
        if (m) lang = m[1];
        buf.push('\n\n```' + lang + '\n' + raw + '\n```\n\n');
        return;
      }
      if (/^H[1-6]$/.test(tag)) buf.push('\n\n' + '#'.repeat(parseInt(tag[1], 10)) + ' ');
      else if (tag === 'LI') buf.push('\n- ');
      else if (tag === 'BLOCKQUOTE') buf.push('\n\n> ');
      else if (tag === 'TR') buf.push('\n| ');
      else if (tag === 'TD' || tag === 'TH') buf.push(' | ');
      else if (tag === 'HR') { buf.push('\n\n---\n\n'); return; }
      else if (['P', 'DIV', 'SECTION', 'ARTICLE', 'UL', 'OL', 'TABLE', 'TBODY', 'THEAD', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6'].indexOf(tag) >= 0) buf.push('\n');
      for (let i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
      if (['P', 'DIV', 'SECTION', 'ARTICLE', 'LI', 'BLOCKQUOTE', 'TR'].indexOf(tag) >= 0) buf.push('\n');
    };
    walk(root);
    let s = buf.join('');
    s = s.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n');
    s = s.replace(/(^|\n)[ \t]*-[ \t]*\n+/g, '$1- ');
    s = s.replace(/^\s+|\s+$/g, '');
    return s;
  };

  const attrSig = (n) => {
    let cls = '';
    const c = n.getAttribute && n.getAttribute('class');
    if (typeof c === 'string') cls = c;
    const extra = ['data-role', 'data-testid', 'aria-label', 'data-message-author-role', 'id']
      .map((k) => (n.getAttribute && n.getAttribute(k)) || '').join(' ');
    return (cls + ' ' + extra).toLowerCase();
  };

  const roleOf = (el) => {
    let n = el, depth = 0;
    while (n && n !== document.body && depth < 6) {
      const s = attrSig(n);
      if (/(^|[\s_-])(user|human|question|query|prompt)([\s_-]|$)/.test(s) || s.indexOf('message-user') >= 0) return 'user';
      if (/(assistant|answer|response|bot|ai-message|message-ai|model|reply|ds-markdown|markdown)/.test(s)) return 'assistant';
      n = n.parentElement; depth++;
    }
    // DeepSeek 分享页没有语义化的角色类名：AI 回答用 markdown 容器渲染，用户提问是纯文本
    try {
      if (el.querySelector && el.querySelector('.ds-markdown, [class*="ds-markdown"], [class*="assistant-message"]')) {
        return 'assistant';
      }
    } catch (e) {}
    return '';
  };

  // 收集候选节点
  const seen = [];
  for (let i = 0; i < selectors.length; i++) {
    let list = [];
    try { list = Array.prototype.slice.call(document.querySelectorAll(selectors[i])); } catch (e) { continue; }
    for (let j = 0; j < list.length; j++) {
      const el = list[j];
      if (seen.indexOf(el) >= 0) continue;
      if (JS_CHROME(el)) continue;
      const txt = (el.innerText || '').trim();
      if (txt.length < 2) continue;
      seen.push(el);
    }
  }

  // 去重：语义化"单条消息"节点优先；包裹多条消息的列表容器丢掉；其余子节点丢掉
  const ATOMIC_RE = /(^|[\s_-])(ds-)?message([\s_-]|$)|assistant-message|chat-item|chatmessage/i;
  const atomicFlags = seen.map((el) => ATOMIC_RE.test(attrSig(el)));
  const containsCount = (el) => {
    let c = 0;
    for (let i = 0; i < seen.length; i++) {
      if (seen[i] !== el && el.contains(seen[i])) c++;
    }
    return c;
  };
  const dropFlags = seen.map((el, i) => {
    if (atomicFlags[i]) {
      let innerAtomic = 0;
      let outerAtomic = false;
      for (let j = 0; j < seen.length; j++) {
        if (j === i || !atomicFlags[j]) continue;
        if (el.contains(seen[j])) innerAtomic++;
        if (seen[j].contains(el)) outerAtomic = true;
      }
      if (innerAtomic >= 2) return true;   // 形如 xxx-message-list 的容器，交给内层
      return outerAtomic;                  // 被外层消息节点包住，交给外层（保留完整内容）
    }
    const own = containsCount(el);
    if (own >= 2) return true;
    if (own === 1) {
      for (let j = 0; j < seen.length; j++) {
        if (j !== i && el.contains(seen[j]) && atomicFlags[j]) return true;
      }
      return false;
    }
    for (let j = 0; j < seen.length; j++) {
      const other = seen[j];
      if (other !== el && other.contains(el)) {
        if (atomicFlags[j]) return true;
        if (containsCount(other) < 2) return true;
      }
    }
    return false;
  });
  const kept = seen.filter((el, i) => !dropFlags[i]);

  kept.sort((a, b) => {
    const rel = a.compareDocumentPosition(b);
    return rel & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
  });

  const blocks = [];
  for (let i = 0; i < kept.length; i++) {
    const el = kept[i];
    let text = '';
    try { text = toMd(el); } catch (e) { text = (el.innerText || ''); }
    text = text.trim();
    if (!text) continue;
    blocks.push({ role: roleOf(el), text: text, len: text.length });
  }

  out.blocks = blocks;
  out.pageText = ((document.body && document.body.innerText) || '').slice(0, 4000);
  return out;
}
"""

# 读取页面内嵌的状态数据（Next.js SSR / __NEXT_DATA__ / 全局 store）
EMBEDDED_JS = r"""
() => {
  const chunks = [];
  const push = (s) => {
    if (typeof s === 'string' && s.length > 40 && s.length < 4_000_000) chunks.push(s);
  };
  if (window.__NEXT_DATA__) { try { push(JSON.stringify(window.__NEXT_DATA__)); } catch (e) {} }
  if (window.__INITIAL_STATE__) { try { push(JSON.stringify(window.__INITIAL_STATE__)); } catch (e) {} }
  if (window.__NUXT__) { try { push(JSON.stringify(window.__NUXT__)); } catch (e) {} }
  const scripts = document.querySelectorAll('script');
  for (let i = 0; i < scripts.length; i++) {
    const t = scripts[i].textContent || '';
    if (!t) continue;
    if (!/message|chat|conversation|share|fragment/i.test(t)) continue;
    push(t);
  }
  return chunks.slice(0, 30);
}
"""


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def _now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_dirs() -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def _clean_url(raw: str) -> str:
    url = (raw or "").strip().strip('"').strip("'")
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    # 去掉复制链接时常见的尾巴字符
    url = re.sub(r"[，。,;；)）\]】]+$", "", url)
    return url


def _is_share_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return "deepseek.com" in host and "/share/" in url


def _slugify(text: str, fallback: str = "deepseek-share") -> str:
    text = (text or "").strip()
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    # 避免文件名过长导致 Windows 路径问题
    return (text[:48] or fallback)


def _norm_role(role: Any) -> str:
    r = str(role or "").lower()
    if not r:
        return ""
    if any(k in r for k in ("user", "human", "question", "asker", "我")):
        return "user"
    if any(k in r for k in ("assistant", "ai", "bot", "model", "deepseek", "answer", "reply")):
        return "assistant"
    return ""


# ----------------------------------------------------------------------------
# JSON 降级解析（第 1.5 / 2 层共用）
# ----------------------------------------------------------------------------
ROLE_KEYS = ("role", "sender", "author", "from", "roleType", "message_role")
CONTENT_KEYS = ("content", "text", "markdown", "answer", "message", "value", "delta")


def _pick(obj: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


def _fragment_text(frag: Any) -> str:
    """把 fragment 转成文本；思考片段用引用块包裹。"""
    if isinstance(frag, str):
        return frag.strip()
    if not isinstance(frag, dict):
        return ""
    raw = _pick(frag, ("content", "text", "value", "delta"))
    if isinstance(raw, list):
        raw = _fragment_text(raw)
    if not isinstance(raw, str) or not raw.strip():
        return ""
    ftype = str(frag.get("type") or frag.get("fragment_type") or "").upper()
    if "THINK" in ftype or "REASON" in ftype:
        return "> 思考过程：\n> " + raw.strip().replace("\n", "\n> ")
    return raw.strip()


def _msg_text(item: dict) -> str:
    """从一条消息对象里尽量挖出正文（正文与 fragments 取信息量更大的那个）。"""
    plain = ""
    for key in CONTENT_KEYS:
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            plain = v.strip()
            break
        if isinstance(v, list):
            joined = "\n\n".join(t for t in (_fragment_text(x) for x in v) if t)
            if joined.strip():
                plain = joined.strip()
                break
        if isinstance(v, dict):
            nested = _msg_text(v)
            if nested:
                plain = nested
                break

    frag_text = ""
    for key in ("fragments", "content_fragments", "parts", "blocks", "contents"):
        frags = item.get(key)
        if isinstance(frags, list) and frags:
            joined = "\n\n".join(t for t in (_fragment_text(x) for x in frags) if t)
            if joined.strip():
                frag_text = joined.strip()
                break

    if len(frag_text) > len(plain):
        return frag_text
    return plain or frag_text


def _iter_message_lists(obj: Any, depth: int = 0):
    """在任意深度的 JSON 中寻找"消息数组"。"""
    if depth > 12:
        return
    if isinstance(obj, list):
        sample = [x for x in obj[:8] if isinstance(x, dict)]
        if sample and len(sample) >= max(1, len(obj[:8]) // 2):
            role_hits = sum(1 for x in sample if _pick(x, ROLE_KEYS) is not None)
            text_hits = sum(1 for x in sample if _msg_text(x))
            if role_hits >= max(1, len(sample) // 2) and text_hits >= 1:
                yield obj
        for x in obj:
            yield from _iter_message_lists(x, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_message_lists(v, depth + 1)


def extract_blocks_from_json(payload: Any) -> list[tuple[str, str]]:
    """从任意 JSON 载荷中提取 [(role, text)]，取信息量最大的一组。"""
    best: list[tuple[str, str]] = []
    best_score = 0
    for lst in _iter_message_lists(payload):
        blocks: list[tuple[str, str]] = []
        for item in lst:
            if not isinstance(item, dict):
                continue
            text = _msg_text(item)
            if not text:
                continue
            role = _norm_role(_pick(item, ROLE_KEYS)) or ""
            blocks.append((role, text))
        if not blocks:
            continue
        score = sum(len(t) for _, t in blocks) + 60 * len(blocks)
        if score > best_score:
            best, best_score = blocks, score
    return best


# ---------------------------------------------------------------------------
# 消息树还原：分享接口会返回"整棵会话树"（含重新生成/编辑产生的所有分支），
# 直接取列表会把所有分支混在一起。这里按 message_id / parent_id 还原"当前链路"。
# ---------------------------------------------------------------------------
TREE_LIST_KEYS = ("messages", "chat_messages", "history", "message_list")


def _iter_message_trees(obj: Any, depth: int = 0):
    if depth > 10:
        return
    if isinstance(obj, dict):
        for key in TREE_LIST_KEYS:
            lst = obj.get(key)
            if isinstance(lst, list) and len(lst) >= 2:
                sample = [m for m in lst[:6] if isinstance(m, dict)]
                if sample and all(("parent_id" in m and "message_id" in m) for m in sample):
                    yield lst
        for v in obj.values():
            yield from _iter_message_trees(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_message_trees(v, depth + 1)


def _tree_to_blocks(lst: list) -> list[tuple[str, str]]:
    by_id = {str(m.get("message_id")): m for m in lst if isinstance(m, dict)}
    child_ids = {str(m.get("parent_id")) for m in lst if isinstance(m, dict)}
    leaves = [m for m in lst if isinstance(m, dict) and str(m.get("message_id")) not in child_ids]
    if not leaves:
        return []

    def _ts(m: dict) -> float:
        try:
            return float(m.get("inserted_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    # 最新创建的那条分支，就是用户最终分享出来的"当前对话"
    leaves.sort(key=_ts)
    node: Any = leaves[-1]
    chain: list[dict] = []
    guard: set[str] = set()
    while isinstance(node, dict):
        mid = str(node.get("message_id"))
        if mid in guard:
            break
        guard.add(mid)
        chain.append(node)
        node = by_id.get(str(node.get("parent_id")))
    chain.reverse()

    blocks: list[tuple[str, str]] = []
    for msg in chain:
        text = _msg_text(msg)
        if not text:
            continue
        blocks.append((_norm_role(msg.get("role")), text))
    return blocks


def extract_tree_blocks(payloads: list[Any]) -> list[tuple[str, str]] | None:
    """在所有载荷里找消息树，返回最长的一条完整链路。"""
    best: list[tuple[str, str]] = []
    best_total = 0
    for payload in payloads:
        for lst in _iter_message_trees(payload):
            blocks = _tree_to_blocks(lst)
            if not blocks:
                continue
            total = sum(len(t) for _, t in blocks)
            if total > best_total:
                best, best_total = blocks, total
    return best or None


# ---------------------------------------------------------------------------
# 用页面上的消息做"锚点"，在消息树里还原页面真正展示的那条分支。
# 会话树里有大量"重新生成/编辑"留下的旁支，只按时间取最新叶节点会选错。
# ---------------------------------------------------------------------------
def _squash_text(text: str) -> str:
    text = re.sub(r"已思考（用时[^）]*）", "", text)
    text = re.sub(r"（用时[^）]*）", "", text)
    text = re.sub(r"本回答由\s*AI\s*生成[^\n]*", "", text)
    text = re.sub(r"内容由\s*AI\s*生成[^\n]*", "", text)
    return re.sub(r"[\s>\-#*`|]+", "", text)


def _index_messages(messages: list[dict]) -> tuple[str, list[tuple[int, int, dict]]]:
    parts, spans, pos = [], [], 0
    for msg in messages:
        text = _squash_text(_msg_text(msg))
        if not text:
            continue
        parts.append(text)
        spans.append((pos, pos + len(text), msg))
        pos += len(text) + 1
    return "\x00".join(parts), spans


def _locate_message(index: int, spans: list[tuple[int, int, dict]]) -> dict | None:
    lo, hi = 0, len(spans) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, msg = spans[mid]
        if index < start:
            hi = mid - 1
        elif index >= end:
            lo = mid + 1
        else:
            return msg
    return None


def _walk_chain(by_id: dict[str, dict], node: dict) -> list[dict]:
    chain, guard = [], set()
    while isinstance(node, dict):
        mid = str(node.get("message_id"))
        if mid in guard:
            break
        guard.add(mid)
        chain.append(node)
        node = by_id.get(str(node.get("parent_id")))
    chain.reverse()
    return chain


def reconstruct_branch(payloads: list[Any], anchors: list[tuple[str, str]]) -> list[tuple[str, str]] | None:
    """anchors 是页面上实际渲染的消息（正序）。用它们在树里定位分支并回溯整条链路。"""
    for payload in payloads:
        for lst in _iter_message_trees(payload):
            msgs = [m for m in lst if isinstance(m, dict)]
            big, spans = _index_messages(msgs)
            if not big or not spans:
                continue
            by_id = {str(m.get("message_id")): m for m in msgs}
            matched: list[dict] = []
            for _role, text in anchors:
                squashed = _squash_text(text)
                if len(squashed) < 12:
                    continue
                start = int(len(squashed) * 0.25)
                probe = squashed[start:start + 60] or squashed[:60]
                idx = big.find(probe)
                if idx < 0 and len(squashed) >= 24:
                    idx = big.find(squashed[:40])
                if idx < 0:
                    continue
                msg = _locate_message(idx, spans)
                if msg is not None:
                    matched.append(msg)
            if not matched:
                continue

            anchor_msg = matched[-1]
            chain = _walk_chain(by_id, anchor_msg)

            # 如果"最新叶节点"是锚点的后继（页面底部还没渲染出最后一条回答），
            # 就用它把链路补到最新。
            child_ids = {str(m.get("parent_id")) for m in msgs}
            leaves = [m for m in msgs if str(m.get("message_id")) not in child_ids]

            def _ts(m: dict) -> float:
                try:
                    return float(m.get("inserted_at") or 0)
                except (TypeError, ValueError):
                    return 0.0

            if leaves:
                newest = max(leaves, key=_ts)
                newest_chain = _walk_chain(by_id, newest)
                if str(anchor_msg.get("message_id")) in {str(m.get("message_id")) for m in newest_chain}:
                    chain = newest_chain

            blocks: list[tuple[str, str]] = []
            for msg in chain:
                text = _msg_text(msg)
                if text:
                    blocks.append((_norm_role(msg.get("role")), text))
            total = sum(len(t) for _, t in blocks)
            if total >= 200:
                return blocks
    return None


def _parse_embedded_payloads(chunks: list[str]) -> list[Any]:
    """把内嵌 script 文本尽量解析成 JSON 对象（含 Next.js 的转义字符串）。"""
    payloads: list[Any] = []
    decoder = json.JSONDecoder()

    def _walk_strings(obj: Any, depth: int = 0):
        if depth > 6 or len(payloads) > 400:
            return
        if isinstance(obj, str):
            s = obj.strip()
            if len(s) > 40 and s[:1] in "{[":
                try:
                    add(json.loads(s))
                except Exception:
                    pass
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk_strings(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                _walk_strings(v, depth + 1)

    def add(obj: Any) -> None:
        if not isinstance(obj, (dict, list)):
            return
        payloads.append(obj)
        # 解析结果里还可能嵌套"字符串化的 JSON"
        _walk_strings(obj)

    for chunk in chunks:
        s = chunk.strip()
        if not s:
            continue
        if s[0] in "{[":
            try:
                add(json.loads(s))
                continue
            except Exception:
                pass
        # 从文本里扫描所有可解析的 JSON 片段
        for m in re.finditer(r"[\[{]", s):
            try:
                obj, _end = decoder.raw_decode(s[m.start():])
            except Exception:
                continue
            add(obj)
        # Next.js 的转义字符串（self.__next_f.push([1,"{\"..."])）
        for m in re.finditer(r'"((?:[^"\\]|\\.){200,})"', s):
            try:
                unescaped = json.loads('"' + m.group(1) + '"')
            except Exception:
                continue
            if unescaped[:1] in "{[":
                try:
                    add(json.loads(unescaped))
                except Exception:
                    pass
        if len(payloads) > 400:
            break
    return payloads


# ----------------------------------------------------------------------------
# 抓取结果
# ----------------------------------------------------------------------------
@dataclass
class ScrapeResult:
    ok: bool
    markdown: str = ""
    strategy: str = ""
    title: str = ""
    message: str = ""
    logs: list[str] = field(default_factory=list)
    saved_path: Path | None = None
    url: str = ""


# ----------------------------------------------------------------------------
# 核心抓取器
# ----------------------------------------------------------------------------
class DeepSeekScraper:
    def __init__(
        self,
        url: str,
        *,
        dom_wait: int = 10,
        net_wait: int = 10,
        allow_headed: bool = True,
        save_debug: bool = True,
        timeout_total: int = 180,
        log: LogFn | None = None,
    ) -> None:
        self.url = url
        self.dom_wait = max(3, int(dom_wait))
        self.net_wait = max(3, int(net_wait))
        self.allow_headed = allow_headed
        self.save_debug = save_debug
        self.deadline = time.time() + timeout_total
        self._log_fn = log
        self.logs: list[str] = []
        self._soft_dom: list[dict] | None = None
        self._page_title = ""
        self._page_text = ""
        self._net: list[dict] = []
        self._html_dump = ""

    # ---------------- 日志 ----------------
    def log(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        self.logs.append(line)
        print(line, flush=True)
        if self._log_fn:
            try:
                self._log_fn(line)
            except Exception:
                pass

    def _time_left(self) -> float:
        return self.deadline - time.time()

    # ---------------- 对外入口 ----------------
    def run(self) -> ScrapeResult:
        _ensure_dirs()
        if not self.url:
            return ScrapeResult(False, message="链接为空", logs=self.logs)

        self.log("========== 开始抓取 ==========")
        self.log(f"目标链接: {self.url}")
        if not _is_share_url(self.url):
            self.log("提示：该链接不像 DeepSeek 分享链接（应形如 https://chat.deepseek.com/share/xxx），仍会尝试。")

        plans: list[dict] = [
            {"name": "方案A｜无头浏览器 + 反检测伪装", "headless": True, "warmup": False, "channel": None, "ua": DESKTOP_UA},
            {"name": "方案B｜无头 + 主站预热(拿Cookie) + 滚动懒加载", "headless": True, "warmup": True, "channel": None, "ua": DESKTOP_UA},
        ]
        if self.allow_headed:
            plans.append(
                {"name": "方案C｜有头真实浏览器(Edge/Chrome)", "headless": False, "warmup": True, "channel": "msedge", "ua": DESKTOP_UA}
            )
        plans.append(
            {"name": "方案D｜无头 + 移动端UA + 刷新重试", "headless": True, "warmup": True, "channel": None, "ua": MOBILE_UA}
        )

        last_error = ""
        for idx, plan in enumerate(plans, start=1):
            if self._time_left() <= 5:
                self.log("总耗时已达上限，停止后续重试。")
                break
            self.log(f"----- 尝试 [{idx}/{len(plans)}] {plan['name']} -----")
            try:
                blocks, strategy, err = self._run_attempt(plan)
            except Exception as exc:  # 兜底：任何异常都不让 UI 崩溃
                blocks, strategy, err = None, "", f"{type(exc).__name__}: {exc}"
                self.log(f"[异常] {err}")
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                    self.log("浏览器内核缺失，请在终端执行：python -m playwright install chromium")
                    last_error = "Playwright 浏览器内核未安装：请运行 python -m playwright install chromium"
                    break

            if blocks:
                problem = self._content_problem(blocks)
                if problem:
                    blocks, strategy, err = None, "", problem
                    self.log(f"内容校验未通过：{problem}")
                else:
                    markdown, title = self._build_markdown(blocks, strategy)
                    self.log(f"抓取成功，策略：{strategy}，正文 {len(markdown)} 字符，块数 {len(blocks)}")
                    path = self._save(markdown, title, strategy)
                    self.log(f"已保存: {path}")
                    if self.save_debug:
                        self._dump_debug(tag="success")
                    return ScrapeResult(
                        True, markdown=markdown, strategy=strategy, title=title,
                        message="抓取成功", logs=self.logs, saved_path=path, url=self.url,
                    )

            last_error = err or "所有策略均未取到内容"
            self.log(f"本次尝试失败：{last_error}")
            if self.save_debug:
                self._dump_debug(tag=f"fail{idx}")

        self.log("========== 抓取结束：失败 ==========")
        hint = (
            "未能提取到对话内容。可能原因：分享已失效/被删除、需要登录、触发风控验证、或页面结构再次改版。\n"
            "建议：勾选\"保留调试文件\"后重试，把 debug_artifacts/ 里的 HTML 与 network JSON 拿出来对照【排故与填坑手册】排查。"
        )
        return ScrapeResult(False, message=last_error or hint, logs=self.logs, url=self.url)

    # ---------------- 单次尝试：三层阶梯 ----------------
    def _run_attempt(self, plan: dict) -> tuple[list[tuple[str, str]] | None, str, str]:
        from playwright.sync_api import sync_playwright

        self._net = []
        self._soft_dom = None
        err = ""

        with sync_playwright() as pw:
            browser = None
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--lang=zh-CN",
                "--disable-dev-shm-usage",
                "--window-size=1366,900",
            ]
            # 优先尝试系统里真实的 Edge / Chrome（比 headless shell 更难被识别）
            if plan.get("channel"):
                try:
                    browser = pw.chromium.launch(headless=plan["headless"], channel=plan["channel"], args=launch_args)
                    self.log(f"已启动系统浏览器通道: {plan['channel']}")
                except Exception as exc:
                    self.log(f"通道 {plan['channel']} 启动失败({exc})，回退内置 Chromium")
                    browser = None
            if browser is None:
                browser = pw.chromium.launch(headless=plan["headless"], args=launch_args)
                self.log(f"已启动内置 Chromium（headless={plan['headless']}）")

            context = browser.new_context(
                user_agent=plan["ua"],
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1366, "height": 900},
                device_scale_factor=1,
                java_script_enabled=True,
            )
            context.add_init_script(STEALTH_JS)
            # 注意：只加 Accept-Language。不要伪造 Sec-CH-UA-* 等客户端提示头，
            # 实测会导致 fe-static.deepseek.com 的 JS 资源返回 ERR_FAILED，页面白屏报"资源加载异常"。
            context.set_extra_http_headers({"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})

            page = context.new_page()
            page.set_default_timeout(45000)

            # 第 2 层的关键：必须在导航之前注册监听，否则会漏掉首屏接口
            self._attach_network_recorder(page)

            try:
                if plan.get("warmup"):
                    self.log("预热：先访问 chat.deepseek.com 主站，获取 Cookie / 通过风控")
                    try:
                        page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(1800)
                        self.log(f"预热完成，页面标题: {page.title()!r}")
                    except Exception as exc:
                        self.log(f"预热失败（不致命）: {type(exc).__name__}: {exc}")

                self.log("导航到分享页 …")
                try:
                    page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
                except Exception as exc:
                    self.log(f"页面导航异常（继续尝试读取现有内容）: {type(exc).__name__}: {exc}")
                    err = f"导航失败: {exc}"

                # 长对话：页面是虚拟列表，DOM 只渲染可视区域，接口里的消息树又可能缺节点。
                # 最可靠的做法是整页滚动、逐屏提取并合并。
                try:
                    page.wait_for_selector(
                        ".ds-message, [class*='ds-message']", timeout=15000, state="attached"
                    )
                except Exception:
                    self.log("【第1层】15s 内未出现消息节点，继续按常规流程尝试")
                if self._page_is_virtualized(page):
                    self._wait_network(4)
                    swept = self._tier_sweep(page)
                    if swept:
                        total = sum(len(t) for _, t in swept)
                        self.log(f"【第1层】整页滚动完成：{len(swept)} 条 / {total} 字符")
                        return swept, "第1层｜虚拟列表整页滚动", ""
                    anchors = self._tier_anchor(page)
                    branch = reconstruct_branch([n["json"] for n in self._net], anchors) if anchors else None
                    if branch:
                        total = sum(len(t) for _, t in branch)
                        self.log(f"【第2层】锚点还原分支：{len(branch)} 条 / {total} 字符")
                        return branch, "第2层｜接口还原当前分支", ""

                # 第 1 层
                blocks = self._tier1_dom(page)
                if blocks:
                    # 长对话是虚拟列表，DOM 只渲染可视区域；此时接口数据往往更完整
                    api_blocks = self._blocks_from_payloads(
                        [n["json"] for n in self._net], layer="第2层(补充)"
                    )
                    if api_blocks:
                        dom_total = sum(len(t) for _, t in blocks)
                        api_total = sum(len(t) for _, t in api_blocks)
                        if api_total > dom_total * 1.2:
                            self.log(
                                f"【第1层】页面为虚拟列表，DOM 仅渲染 {len(blocks)} 条（{dom_total} 字符）；"
                                f"接口更完整（{len(api_blocks)} 条 / {api_total} 字符），改用接口数据"
                            )
                            return api_blocks, "第2层｜分享接口完整会话（长对话优先）", ""
                    return blocks, "第1层｜DOM 选择器提取", ""

                # 第 1.5 层
                blocks = self._tier15_embedded(page)
                if blocks:
                    return blocks, "第1.5层｜内嵌状态数据(SSR JSON)", ""

                # 第 2 层
                blocks = self._tier2_network(page)
                if blocks:
                    return blocks, "第2层｜Network JSON 接口监听", ""

                # 第 3 层：强制等待 + 滚动 + 刷新重试
                blocks = self._tier3_retry(page)
                if blocks:
                    return blocks, "第3层｜强制等待/滚动/刷新重试", ""

                # 有头模式再给一次人工通过风控的机会
                if not plan.get("headless"):
                    self.log("有头模式下额外等待 15 秒，若浏览器窗口出现验证码，请手动完成 …")
                    page.wait_for_timeout(15000)
                    blocks = self._tier1_dom(page, wait_seconds=6)
                    if blocks:
                        return blocks, "第3层｜有头浏览器 + 人工通过验证", ""
                    blocks = self._tier2_network(page)
                    if blocks:
                        return blocks, "第2层｜人工通过验证后 Network 抓取", ""

                return None, "", err or self._diagnose_page()
            finally:
                try:
                    self._html_dump = page.content()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

    # ---------------- 第 1 层：DOM ----------------
    def _page_is_virtualized(self, page) -> bool:
        try:
            return bool(
                page.evaluate(
                    """() => {
                        const vl = document.querySelector('.ds-virtual-list');
                        return !!vl && vl.scrollHeight > vl.clientHeight * 3;
                    }"""
                )
            )
        except Exception:
            return False

    def _wait_network(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._net:
            time.sleep(0.3)

    def _tier_sweep(self, page) -> list[tuple[str, str]] | None:
        """虚拟列表整页滚动：从上往下逐屏提取并合并，直到真正滚到底且总高度稳定。"""
        self.log("【第1层】检测到虚拟列表，改为整页滚动逐屏提取 …")
        original_viewport = page.viewport_size
        try:
            # 加高视口，一屏能装下更多消息，滚动次数成倍减少
            page.set_viewport_size({"width": 1366, "height": 4800})
        except Exception:
            pass
        try:
            return self._sweep_loop(page)
        finally:
            try:
                if original_viewport:
                    page.set_viewport_size(original_viewport)
            except Exception:
                pass

    def _sweep_loop(self, page) -> list[tuple[str, str]] | None:
        try:
            page.evaluate(
                "() => { const vl=document.querySelector('.ds-virtual-list'); if (vl) vl.scrollTop = 0; }"
            )
            page.wait_for_timeout(800)
        except Exception:
            pass

        seen: set[str] = set()
        merged: list[tuple[str, str]] = []
        stable = 0
        deadline = time.time() + 600

        def collect() -> None:
            data = self._evaluate_extract(page)
            if not data:
                return
            self._remember_page(data)
            for role, text in self._postprocess_blocks(data.get("blocks") or []) or []:
                key = _squash_text(text)[:80] + "|" + role
                if key in seen:
                    continue
                seen.add(key)
                merged.append((role, text))

        last_max = -1.0
        for step in range(1, 3001):
            if time.time() > deadline:
                self.log("【第1层】滚动已达 600s 上限，停止并保存已抓取内容")
                break
            collect()
            try:
                info = page.evaluate(
                    """() => {
                        const vl = document.querySelector('.ds-virtual-list');
                        if (!vl) {
                            window.scrollBy(0, Math.round(window.innerHeight * 0.8));
                            return {top: window.scrollY, max: document.body.scrollHeight - window.innerHeight,
                                    h: document.body.scrollHeight, vl: false};
                        }
                        const step = Math.round(vl.clientHeight * 1.0) || 800;
                        vl.scrollTop = Math.min(vl.scrollHeight, vl.scrollTop + step);
                        return {top: vl.scrollTop, max: vl.scrollHeight - vl.clientHeight,
                                h: vl.scrollHeight, vl: true};
                    }"""
                )
            except Exception:
                break

            at_bottom = info["top"] >= info["max"] - 2
            if at_bottom:
                # 虚拟列表的总高度会随消息真正渲染而增长，到底后再观察两轮
                page.wait_for_timeout(700)
                if abs(info["h"] - last_max) < 1:
                    stable += 1
                else:
                    stable = 0
                last_max = info["h"]
                if stable >= 2:
                    collect()
                    break
            else:
                stable = 0
                last_max = info["h"]
                page.wait_for_timeout(100)

            if step % 25 == 0:
                pct = 100 * info["top"] / max(info["max"], 1)
                self.log(f"【第1层】滚动中：{pct:.0f}%，已收集 {len(merged)} 条消息 …")

        return merged or None

    def _tier_anchor(self, page) -> list[tuple[str, str]]:
        """滚到对话底部，取当前可见消息作为锚点（长对话是虚拟列表，必须滚动才能渲染出末尾）。"""
        try:
            # 先等消息节点真正渲染出来，否则滚动+提取都会拿到空列表
            page.wait_for_function(
                "() => document.querySelectorAll('.ds-message, [class*=\"ds-message\"]').length > 0",
                timeout=15000,
            )
        except Exception:
            self.log("【锚点】15s 内没有等到消息节点渲染，跳过锚点方案")

        for attempt in range(1, 4):
            try:
                page.evaluate(
                    """() => {
                        const vl = document.querySelector('.ds-virtual-list');
                        if (vl) { vl.scrollTop = vl.scrollHeight; return; }
                        window.scrollTo(0, document.body.scrollHeight);
                    }"""
                )
                page.wait_for_timeout(1600)
                data = self._evaluate_extract(page)
                if not data:
                    self.log(f"【锚点】第 {attempt} 次提取返回空")
                    continue
                self._remember_page(data)
                blocks = self._postprocess_blocks(data.get("blocks") or []) or []
                if blocks:
                    self.log(f"【锚点】已取到页面末尾 {len(blocks)} 条消息作为定位锚点")
                    return blocks
                self.log(f"【锚点】第 {attempt} 次未取到文本块，重试 …")
            except Exception as exc:
                self.log(f"【锚点】读取页面末尾消息失败: {type(exc).__name__}: {exc}")
        return []

    def _tier1_dom(self, page, wait_seconds: int | None = None) -> list[tuple[str, str]] | None:
        wait_s = wait_seconds or self.dom_wait
        union = ", ".join(s for group in DOM_SELECTOR_GROUPS for s in group)
        self.log(f"【第1层】等待 DOM 选择器（上限 {wait_s}s）: {union}")
        try:
            page.wait_for_selector(union, timeout=wait_s * 1000, state="attached")
            self.log("【第1层】选择器已命中，开始提取 …")
        except Exception:
            self.log(f"【第1层】{wait_s}s 内未等到任何选择器，仍尝试直接提取现有 DOM")

        # 内容可能分批渲染，做一次短暂的"内容稳定"轮询
        best: list[dict] = []
        best_total = 0
        for round_idx in range(3):
            data = self._evaluate_extract(page)
            if data:
                self._remember_page(data)
                blocks = data.get("blocks") or []
                total = sum(len(b.get("text", "")) for b in blocks)
                self.log(f"【第1层】第 {round_idx + 1} 轮提取：{len(blocks)} 块 / {total} 字符")
                if total > best_total:
                    best, best_total = blocks, total
                if total >= 300 and len(blocks) >= 2:
                    break
            if round_idx < 2 and self._time_left() > 8:
                page.wait_for_timeout(1200)

        processed = self._postprocess_blocks(best)
        if processed:
            return processed

        # 合并式提取失败时，再按选择器分组独立评估（不同角色可能被不同选择器命中）
        return self._extract_by_group(page)

    def _evaluate_extract(self, page) -> dict | None:
        try:
            return page.evaluate(EXTRACT_JS, [s for g in DOM_SELECTOR_GROUPS for s in g])
        except Exception as exc:
            self.log(f"【第1层】页面执行脚本失败: {type(exc).__name__}: {exc}")
            return None

    def _extract_by_group(self, page) -> list[tuple[str, str]] | None:
        """逐组选择器单独评估，避免宽泛选择器把窄结果吞掉。"""
        best: list[tuple[str, str]] = []
        for group in DOM_SELECTOR_GROUPS:
            try:
                data = page.evaluate(EXTRACT_JS, group)
            except Exception:
                continue
            blocks = (data or {}).get("blocks") or []
            if not blocks:
                continue
            total = sum(len(b.get("text", "")) for b in blocks)
            if len(blocks) >= 2 and total >= 80:
                self.log(f"【第1层】分组 {group[:2]} 命中 {len(blocks)} 块 / {total} 字符")
                processed = self._postprocess_blocks(blocks)
                if processed and (len(processed), sum(len(t) for _, t in processed)) > (
                    len(best), sum(len(t) for _, t in best)
                ):
                    best = processed
        return best or None

    def _remember_page(self, data: dict) -> None:
        if data.get("title"):
            self._page_title = data["title"]
        if data.get("pageText"):
            self._page_text = data["pageText"]

    def _postprocess_blocks(self, raw_blocks: list[dict]) -> list[tuple[str, str]] | None:
        cleaned: list[tuple[str, str]] = []
        prev_text = ""
        for b in raw_blocks:
            text = (b.get("text") or "").strip()
            if not text:
                continue
            if len(text) <= 12 and text in NOISE_TEXTS:
                continue
            if text == prev_text:  # 同一节点被多个选择器重复命中
                continue
            cleaned.append((_norm_role(b.get("role")), text))
            prev_text = text

        if not cleaned:
            return None

        # 角色补全：有标记的保留，没标记的按"问/答"交替推断
        if any(r for r, _ in cleaned):
            result: list[tuple[str, str]] = []
            last = ""
            for role, text in cleaned:
                if not role:
                    role = "assistant" if last == "user" else "user"
                result.append((role, text))
                last = role
            cleaned = result
        else:
            start_user = not cleaned[0][1].lstrip().startswith(("#", "```"))
            cleaned = [
                (("user" if (i % 2 == 0) == start_user else "assistant"), t)
                for i, (_, t) in enumerate(cleaned)
            ]

        # 过滤掉明显是导航/页脚的残余
        cleaned = [(r, t) for r, t in cleaned if len(t) >= 2]
        if not cleaned:
            return None
        if sum(len(t) for _, t in cleaned) < 40:
            return None
        return cleaned

    # ---------------- 第 1.5 层：内嵌 JSON ----------------
    def _tier15_embedded(self, page) -> list[tuple[str, str]] | None:
        self.log("【第1.5层】DOM 未取到有效内容，尝试解析页面内嵌状态数据（__NEXT_DATA__ / SSR JSON）")
        try:
            chunks = page.evaluate(EMBEDDED_JS) or []
        except Exception as exc:
            self.log(f"【第1.5层】读取 script 失败: {type(exc).__name__}: {exc}")
            return None
        self.log(f"【第1.5层】捕获 {len(chunks)} 段候选脚本，开始 JSON 解析 …")
        payloads = _parse_embedded_payloads(chunks)
        self.log(f"【第1.5层】成功解析 {len(payloads)} 个 JSON 对象/数组")
        return self._blocks_from_payloads(payloads, layer="第1.5层")

    def _blocks_from_payloads(self, payloads: list[Any], layer: str) -> list[tuple[str, str]] | None:
        # 优先按"消息树"还原当前对话链路，避免把重新生成/编辑产生的分支混进来
        tree_blocks = extract_tree_blocks(payloads)
        if tree_blocks:
            total = sum(len(t) for _, t in tree_blocks)
            if total >= 40:
                self.log(f"【{layer}】识别到消息树，已还原当前链路：{len(tree_blocks)} 条 / {total} 字符")
                return tree_blocks

        best: list[tuple[str, str]] = []
        best_score = 0
        for payload in payloads:
            blocks = extract_blocks_from_json(payload)
            if not blocks:
                continue
            score = sum(len(t) for _, t in blocks) + 60 * len(blocks)
            if score > best_score:
                best, best_score = blocks, score
        if not best:
            self.log(f"【{layer}】未在 JSON 中找到消息数组")
            return None
        if sum(len(t) for _, t in best) < 40:
            self.log(f"【{layer}】JSON 中的文本过短，放弃")
            return None
        self.log(f"【{layer}】提取成功：{len(best)} 条消息 / {sum(len(t) for _, t in best)} 字符")
        return best

    # ---------------- 第 2 层：Network ----------------
    def _attach_network_recorder(self, page) -> None:
        def on_response(resp) -> None:
            try:
                req = resp.request
                if req.resource_type not in ("xhr", "fetch", "document"):
                    return
                if len(self._net) >= 80:
                    return
                headers = resp.headers or {}
                ctype = (headers.get("content-type") or "").lower()
                if "json" not in ctype:
                    return
                try:
                    body = resp.json()
                except Exception:
                    return
                self._net.append({"url": resp.url, "status": resp.status, "json": body})
            except Exception:
                pass

        self._net = []
        page.on("response", on_response)
        self.log("已挂载 Network 监听器（后续接口响应会被缓存，用于第2层降级）")

    def _tier2_network(self, page) -> list[tuple[str, str]] | None:
        self.log(f"【第2层】DOM 失败，检查已捕获的接口响应（当前 {len(self._net)} 条 JSON）")
        blocks = self._blocks_from_payloads([n["json"] for n in self._net], layer="第2层")
        if blocks:
            return blocks

        # 主动补一轮：轻量滚动/点击"展开"，给接口更多触发机会
        self.log(f"【第2层】暂未命中，继续等待 {self.net_wait}s 观察是否还有接口返回 …")
        waited = 0
        while waited < self.net_wait * 1000 and self._time_left() > 5:
            page.wait_for_timeout(1000)
            waited += 1000
            if self._net:
                blocks = self._blocks_from_payloads([n["json"] for n in self._net], layer="第2层")
                if blocks:
                    return blocks
        self.log("【第2层】接口监听未获得可解析的对话数据")
        return None

    # ---------------- 第 3 层：重试与兜底 ----------------
    def _tier3_retry(self, page) -> list[tuple[str, str]] | None:
        self.log("【第3层】进入强制等待 + 滚动懒加载 + 刷新重试 …")
        try:
            self.log("【第3层】强制等待 3s（time.sleep 兜底，让前端完成异步渲染）")
            time.sleep(3)
            for i in range(5):
                if self._time_left() <= 10:
                    break
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(900)
                self.log(f"【第3层】滚动到底部 {i + 1}/5")
            page.wait_for_timeout(1500)
        except Exception as exc:
            self.log(f"【第3层】滚动阶段异常: {type(exc).__name__}: {exc}")

        blocks = self._tier1_dom(page, wait_seconds=5)
        if blocks:
            return blocks
        blocks = self._tier2_network(page)
        if blocks:
            return blocks

        self.log("【第3层】刷新页面重试（模拟用户 F5，部分风控会在二次请求放行）")
        self._net = []
        try:
            page.reload(wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(4000)
        except Exception as exc:
            self.log(f"【第3层】刷新失败: {type(exc).__name__}: {exc}")
            return None

        blocks = self._tier1_dom(page, wait_seconds=max(5, self.dom_wait // 2))
        if blocks:
            return blocks
        return self._tier2_network(page)

    # ---------------- 页面诊断 ----------------
    def _content_problem(self, blocks: list[tuple[str, str]]) -> str:
        """内容校验：防止把错误页/首页的界面文案当成对话保存。"""
        page_text = self._page_text or ""
        joined = "\n".join(t for _, t in blocks)
        if re.search(r"(分享|共享).{0,6}(不存在|已失效|已取消|已删除|过期)", page_text + joined):
            return "分享链接已失效/被删除/过期，请重新生成分享链接"
        if re.search(r"资源加载异常|页面加载失败|加载出错|出错了", page_text):
            return "页面提示加载异常（链接无效，或静态资源被网络拦截）"
        if len(blocks) < 2 or len(joined) < 60:
            return "只提取到界面文案，未识别到对话内容（链接不是分享页，或分享已失效）"
        if not any(role for role, _ in blocks):
            return "提取到的内容无法区分提问与回答，可能页面结构已改版"
        return ""

    def _diagnose_page(self) -> str:
        text = self._page_text or ""
        title = self._page_title or ""
        self.log(f"页面标题: {title!r}")
        self.log(f"页面正文前 300 字: {text[:300]!r}")
        if re.search(r"(分享|共享).{0,6}(不存在|已失效|已取消|已删除|过期)", text):
            return "分享链接已失效/被删除/过期，请重新生成分享链接"
        if re.search(r"资源加载异常|页面加载失败|加载失败", text):
            return "页面提示“资源加载异常”：多为分享链接无效/已过期，或前端静态资源被网络拦截（可换网络、勾选有头模式重试）"
        if re.search(r"(安全验证|请完成验证|人机验证|滑动验证|验证码)", text):
            return "触发风控验证。建议：勾选允许有头浏览器重试，在弹出的窗口里手动完成验证"
        if re.search(r"(登录|Log ?in|Sign ?in)", text) and len(text) < 800:
            return "页面要求登录后才能查看，请在浏览器中登录后使用 Cookie 方案（或改用有头模式手动登录）"
        if not text.strip():
            return "页面几乎无文本内容，可能是纯客户端渲染失败或被网络拦截"
        return "页面可访问，但未识别到对话结构（可能已改版，请按手册用 F12 更新选择器/接口）"

    # ---------------- Markdown 组装 / 落盘 ----------------
    def _build_markdown(self, blocks: list[tuple[str, str]], strategy: str) -> tuple[str, str]:
        title = self._derive_title(blocks)
        lines: list[str] = []
        for role, text in blocks:
            label = "User" if role == "user" else "DeepSeek"
            lines.append(f"**{label}**\n\n{text.strip()}\n")
        body = "\n---\n\n".join(lines).strip()

        header = (
            f"---\n"
            f'title: "{title}"\n'
            f'source: "{self.url}"\n'
            f'scraped_at: "{_now_stamp()}"\n'
            f'strategy: "{strategy}"\n'
            f"---\n\n"
            f"> 来源：{self.url}  \n"
            f"> 抓取时间：{_now_stamp()}  \n"
            f"> 抓取策略：{strategy}\n\n"
        )
        markdown = header + body + "\n"
        return markdown, title

    def _derive_title(self, blocks: list[tuple[str, str]]) -> str:
        for role, text in blocks:
            if role == "user" and text.strip():
                first = text.strip().splitlines()[0]
                first = re.sub(r"^[#>\-\*\s]+", "", first)
                if len(first) >= 4:
                    return first[:40]
        for _, text in blocks:
            first = re.sub(r"^[#>\-\*\s]+", "", text.strip().splitlines()[0])
            if len(first) >= 4:
                return first[:40]
        t = re.sub(r"\s*[-|｜].*$", "", self._page_title or "")
        return (t or "DeepSeek 对话")[:40]

    def _save(self, markdown: str, title: str, strategy: str) -> Path:
        _ensure_dirs()
        # 同一分享链接重复抓取时覆盖旧记录，避免大文件重复堆积
        replaced = 0
        for old in MEMORY_DIR.glob("*.md"):
            try:
                head = old.read_text(encoding="utf-8")[:800]
            except Exception:
                continue
            if f'source: "{self.url}"' in head:
                try:
                    old.unlink()
                    replaced += 1
                except OSError:
                    pass
        if replaced:
            self.log(f"已覆盖同一链接的旧记录 {replaced} 个")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = MEMORY_DIR / f"{stamp}_{_slugify(title)}.md"
        suffix = 1
        while path.exists():
            path = MEMORY_DIR / f"{stamp}_{_slugify(title)}_{suffix}.md"
            suffix += 1
        path.write_text(markdown, encoding="utf-8")
        return path

    def _dump_debug(self, tag: str) -> None:
        try:
            _ensure_dirs()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = DEBUG_DIR / f"{stamp}_{tag}"
            if self._html_dump:
                (base.with_name(base.name + "_page.html")).write_text(self._html_dump, encoding="utf-8")
            if self._net:
                slim = []
                for item in self._net:
                    text = json.dumps(item["json"], ensure_ascii=False)
                    slim.append({"url": item["url"], "status": item["status"], "size": len(text),
                                 "preview": text[:2000]})
                (base.with_name(base.name + "_network.json")).write_text(
                    json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            self.log(f"调试文件已写入 {DEBUG_DIR.name}/（{tag}）")
        except Exception as exc:
            self.log(f"写调试文件失败: {type(exc).__name__}: {exc}")


def scrape_deepseek(
    url: str,
    *,
    dom_wait: int = 10,
    allow_headed: bool = True,
    save_debug: bool = True,
    log: LogFn | None = None,
) -> ScrapeResult:
    """供 UI 调用的统一入口。"""
    scraper = DeepSeekScraper(
        _clean_url(url),
        dom_wait=dom_wait,
        allow_headed=allow_headed,
        save_debug=save_debug,
        log=log,
    )
    return scraper.run()


# ----------------------------------------------------------------------------
# 本地记忆库读取
# ----------------------------------------------------------------------------
def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end]
            body = text[end + 4:].lstrip("\n")
            meta: dict[str, str] = {}
            for line in block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
            return meta, body
    return {}, text


def _strip_lead_quote(body: str) -> str:
    """去掉正文开头那段“来源/抓取时间”引用块——卡片头部已经展示过，避免重复。"""
    lines = body.splitlines()
    head = lines[:6]
    if not any(("来源：" in line) or ("抓取策略" in line) for line in head):
        return body.strip()
    i = 0
    while i < len(lines) and (lines[i].startswith(">") or not lines[i].strip()):
        i += 1
    rest = "\n".join(lines[i:]).strip()
    return rest or body.strip()


@st.cache_data(show_spinner=False)
def load_memories(dir_mtime: float) -> list[dict]:
    """dir_mtime 仅用于让缓存随目录变化失效。"""
    _ensure_dirs()
    items: list[dict] = []
    for path in sorted(MEMORY_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            continue
        meta, body = split_front_matter(raw)
        body = _strip_lead_quote(body)
        stat = path.stat()
        items.append(
            {
                "path": str(path),
                "name": path.name,
                "title": meta.get("title") or path.stem,
                "source": meta.get("source", ""),
                "scraped_at": meta.get("scraped_at", ""),
                "strategy": meta.get("strategy", ""),
                "summary": meta.get("summary", ""),
                "body": body.strip(),
                "raw": raw,
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "size": stat.st_size,
                "chars": len(body),
            }
        )
    return items


def _dir_signature() -> float:
    _ensure_dirs()
    files = list(MEMORY_DIR.glob("*.md"))
    return max([p.stat().st_mtime for p in files], default=0.0) + len(files)


# ----------------------------------------------------------------------------
# AI 总结（OpenAI 兼容接口，默认 DeepSeek）
# ----------------------------------------------------------------------------
def _read_ai_config() -> dict:
    cfg = dict(AI_DEFAULTS)
    try:
        if AI_CONFIG_PATH.exists():
            data = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
            for k in cfg:
                if isinstance(data.get(k), str) and data[k].strip():
                    cfg[k] = data[k].strip()
    except Exception:
        pass
    return cfg


def _write_ai_config(cfg: dict) -> None:
    payload = {k: str(cfg.get(k, AI_DEFAULTS[k])).strip() for k in AI_DEFAULTS}
    AI_CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _ai_is_ready(cfg: dict) -> bool:
    return bool(cfg.get("api_key") and cfg.get("base_url") and cfg.get("model"))


def _clip_for_ai(body: str) -> str:
    """超长对话只取头尾，控制 token 成本。"""
    if len(body) <= AI_INPUT_HEAD + AI_INPUT_TAIL:
        return body
    return (
        body[:AI_INPUT_HEAD]
        + "\n\n……（中间省略 " + f"{len(body) - AI_INPUT_HEAD - AI_INPUT_TAIL:,}" + " 字）……\n\n"
        + body[-AI_INPUT_TAIL:]
    )


def _ai_chat(cfg: dict, messages: list[dict], timeout: int = 90, max_tokens: int = 800) -> str:
    """调用 OpenAI 兼容的 /chat/completions，失败抛 RuntimeError。"""
    url = cfg["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = json.dumps(
        {
            "model": cfg["model"],
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise RuntimeError(f"接口返回 {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络无法连接：{exc.reason}") from exc
    except Exception as exc:
        raise RuntimeError(f"请求失败：{type(exc).__name__}: {exc}") from exc

    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"接口返回格式异常：{json.dumps(data, ensure_ascii=False)[:300]}")


def _ai_chat_stream(cfg: dict, messages: list[dict], timeout: int = 120, max_tokens: int = 2000):
    """流式调用，逐块产出正文；用于让界面边生成边显示，避免"卡死"观感。"""
    url = cfg["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = json.dumps(
        {
            "model": cfg["model"],
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "stream": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise RuntimeError(f"接口返回 {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络无法连接：{exc.reason}") from exc
    except Exception as exc:
        raise RuntimeError(f"请求失败：{type(exc).__name__}: {exc}") from exc

    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            piece = (choices[0].get("delta") or {}).get("content") or ""
            if piece:
                yield piece


def summarize_memory(item: dict, cfg: dict) -> str:
    title = item.get("title", "")
    body = _clip_for_ai(item.get("body", ""))
    instruction = (
        "你是一个对话归档助手。请用中文为下面这段对话写一段总结，要求：\n"
        "1. 3-5 句话，总长度 120-220 字，纯文本，不要 Markdown 标题和列表符号；\n"
        "2. 先一句话概括对话主题，再列出关键结论/方案，最后一句说明可复用的要点或结论；\n"
        "3. 只依据对话内容，不要编造；如果对话以代码为主，点明技术栈与核心做法。"
    )
    messages = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": f"对话标题：{title}\n\n对话正文：\n{body}"},
    ]
    summary = _ai_chat(cfg, messages)
    summary = re.sub(r"\s+", " ", summary).strip()
    if not summary:
        raise RuntimeError("接口返回了空内容")
    return summary[:400]


def _set_summary(path: Path, summary: str) -> None:
    """把总结写回 Markdown 的 front matter（单行保存，保证可解析）。"""
    raw = Path(path).read_text(encoding="utf-8")
    meta, body = split_front_matter(raw)
    meta["summary"] = re.sub(r"\s+", " ", summary).strip()
    order = ["title", "source", "scraped_at", "strategy", "summary"]
    keys = [k for k in order if k in meta] + [k for k in meta if k not in order]
    front = "---\n" + "".join(f'{k}: "{meta[k]}"\n' for k in keys) + "---\n\n"
    Path(path).write_text(front + body, encoding="utf-8")


# ----------------------------------------------------------------------------
# 续聊 Prompt：读完整段对话，产出一段可粘贴到新对话的交接提示词
# ----------------------------------------------------------------------------
PROMPT_DIR = MEMORY_DIR / ".prompts"


def _prompt_path(item: dict) -> Path:
    return PROMPT_DIR / (Path(item["name"]).stem + ".txt")


def _read_prompt(item: dict) -> str:
    path = _prompt_path(item)
    try:
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""
    except Exception:
        return ""


def _write_prompt(item: dict, text: str) -> None:
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    _prompt_path(item).write_text(text.strip() + "\n", encoding="utf-8")


def _handoff_messages(item: dict) -> list[dict]:
    title = item.get("title", "")
    body = _clip_for_ai(item.get("body", ""))
    instruction = (
        "你是一个上下文交接专家。下面是一段已经结束的对话，请把它压缩成一段"
        "「可以直接粘贴到全新对话窗口」的接手提示词，让新窗口的 AI 无需阅读原文就能无缝继续。\n"
        "硬性要求：\n"
        "1. 只输出提示词正文本身，不要任何解释、前言、结语，不要用 ``` 包裹；\n"
        "2. 用中文，分小节写，标题用「## 」。必须包含这些小节：\n"
        "   ## 背景与目标 —— 这件事是什么、最终要达成什么；\n"
        "   ## 已完成 —— 已经做完的事、产出的文件/代码/配置（保留具体路径、命令、参数）；\n"
        "   ## 当前状态 —— 现在停在哪一步、已确认的结论和关键数据；\n"
        "   ## 关键约束 —— 用户的偏好、踩过的坑、明确不要做的事；\n"
        "   ## 接下来要做的 —— 按顺序列出剩余任务；\n"
        "3. 保留可复用的具体信息（技术栈、文件名、命令、报错原文），不要泛泛而谈；\n"
        "4. 全文控制在 600 字以内，信息密度优先；\n"
        "5. 如果对话里信息不足以填某一节，就写「（原文未涉及）」，不要编造；\n"
        "6. 必须写完整最后一节，不要在句子中间结束。"
    )
    return [
        {"role": "system", "content": instruction},
        {"role": "user", "content": f"对话标题：{title}\n\n对话正文：\n{body}"},
    ]


def _clean_prompt(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def build_handoff_prompt(item: dict, cfg: dict) -> str:
    """非流式版本（命令行/测试用），界面走流式以便边生成边显示。"""
    text = _clean_prompt(_ai_chat(cfg, _handoff_messages(item), timeout=120, max_tokens=2000))
    if not text:
        raise RuntimeError("接口返回了空内容")
    return text


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 主题：双主题（暗色极客风 / 浅色清爽风），侧边栏一键切换 + 持久化到 config.toml
# ---------------------------------------------------------------------------
STREAMLIT_THEMES: dict[str, dict] = {
    "dark": {
        "base": "dark",
        "primaryColor": "#22d3ee",
        "backgroundColor": "#06090e",
        "secondaryBackgroundColor": "#0b1119",
        "textColor": "#d7e3ee",
    },
    "light": {
        "base": "light",
        "primaryColor": "#0ea5b7",
        "backgroundColor": "#f7f9fc",
        "secondaryBackgroundColor": "#ffffff",
        "textColor": "#1e2936",
    },
    "amber": {
        "base": "dark",
        "primaryColor": "#f5b942",
        "backgroundColor": "#1a120a",
        "secondaryBackgroundColor": "#241810",
        "textColor": "#f4e9d4",
    },
}

GEEK_CSS_VARS: dict[str, dict] = {
    "dark": {
        "geek-bg": "#06090e",
        "geek-panel": "#0b1119",
        "geek-panel-soft": "rgba(11,17,25,.72)",
        "geek-glass": "rgba(11,17,25,.55)",
        "geek-glass-border": "rgba(34,211,238,.18)",
        "geek-line": "#16232f",
        "geek-line-soft": "rgba(34,211,238,.28)",
        "geek-cyan": "#22d3ee",
        "geek-cyan-soft": "rgba(34,211,238,.35)",
        "geek-cyan-glow": "rgba(34,211,238,.55)",
        "geek-green": "#7cf59d",
        "geek-pink": "#ff3d81",
        "geek-text": "#e3edf6",
        "geek-text-strong": "#e9f7ff",
        "geek-dim": "#8fa8bd",
        "geek-input-bg": "#080d13",
        "geek-title-shadow": "rgba(34,211,238,.45)",
        "geek-sidebar-grad": "linear-gradient(180deg,#0a1018 0%,#06090e 100%)",
        "geek-app-radial-a": "rgba(34,211,238,.10)",
        "geek-app-radial-b": "rgba(255,61,129,.07)",
        "geek-card-shadow": "0 2px 8px rgba(0,0,0,.35), 0 0 0 1px rgba(34,211,238,.06)",
        "geek-card-shadow-hover": "0 4px 20px rgba(34,211,238,.12), 0 0 0 1px rgba(34,211,238,.25)",
        "geek-scrollbar": "#1a2836",
        "geek-scrollbar-thumb": "#2a4055",
        "geek-transition": "220ms cubic-bezier(.4,0,.2,1)",
    },
    "light": {
        "geek-bg": "#f7f9fc",
        "geek-panel": "#ffffff",
        "geek-panel-soft": "rgba(255,255,255,.85)",
        "geek-glass": "rgba(255,255,255,.72)",
        "geek-glass-border": "rgba(14,165,183,.15)",
        "geek-line": "#e2e8f0",
        "geek-line-soft": "rgba(14,165,183,.28)",
        "geek-cyan": "#0ea5b7",
        "geek-cyan-soft": "rgba(14,165,183,.32)",
        "geek-cyan-glow": "rgba(14,165,183,.42)",
        "geek-green": "#10b981",
        "geek-pink": "#e11d7d",
        "geek-text": "#1e2936",
        "geek-text-strong": "#0f172a",
        "geek-dim": "#64748b",
        "geek-input-bg": "#f1f5f9",
        "geek-title-shadow": "rgba(14,165,183,.18)",
        "geek-sidebar-grad": "linear-gradient(180deg,#f1f5f9 0%,#f7f9fc 100%)",
        "geek-app-radial-a": "rgba(14,165,183,.05)",
        "geek-app-radial-b": "rgba(225,29,125,.04)",
        "geek-card-shadow": "0 1px 3px rgba(15,23,42,.08), 0 0 0 1px rgba(14,165,183,.04)",
        "geek-card-shadow-hover": "0 4px 16px rgba(14,165,183,.10), 0 0 0 1px rgba(14,165,183,.22)",
        "geek-scrollbar": "#e2e8f0",
        "geek-scrollbar-thumb": "#cbd5e1",
        "geek-transition": "220ms cubic-bezier(.4,0,.2,1)",
    },
    "amber": {
        "geek-bg": "#1a120a",
        "geek-panel": "#241810",
        "geek-panel-soft": "rgba(36,24,16,.78)",
        "geek-glass": "rgba(36,24,16,.60)",
        "geek-glass-border": "rgba(245,185,66,.20)",
        "geek-line": "#3a2818",
        "geek-line-soft": "rgba(245,185,66,.30)",
        "geek-cyan": "#f5b942",
        "geek-cyan-soft": "rgba(245,185,66,.32)",
        "geek-cyan-glow": "rgba(245,185,66,.55)",
        "geek-green": "#6fc47a",
        "geek-pink": "#d95c4f",
        "geek-text": "#f4e9d4",
        "geek-text-strong": "#fff6e2",
        "geek-dim": "#a8947a",
        "geek-input-bg": "#140c06",
        "geek-title-shadow": "rgba(245,185,66,.50)",
        "geek-sidebar-grad": "linear-gradient(180deg,#1e1509 0%,#1a120a 100%)",
        "geek-app-radial-a": "rgba(245,185,66,.10)",
        "geek-app-radial-b": "rgba(217,92,79,.06)",
        "geek-card-shadow": "0 2px 8px rgba(0,0,0,.45), 0 0 0 1px rgba(245,185,66,.08)",
        "geek-card-shadow-hover": "0 4px 20px rgba(245,185,66,.14), 0 0 0 1px rgba(245,185,66,.30)",
        "geek-scrollbar": "#3a2818",
        "geek-scrollbar-thumb": "#5a3e22",
        "geek-transition": "220ms cubic-bezier(.4,0,.2,1)",
    },
}


def _streamlit_config_path() -> Path:
    return BASE_DIR / ".streamlit" / "config.toml"


def _read_config_toml() -> dict:
    """极简 TOML 读取，只关心 [theme] + [client] 两段。"""
    cfg = {"theme": {}, "client": {}}
    path = _streamlit_config_path()
    if not path.exists():
        return cfg
    section = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        val = val.strip('"').strip("'")
        cfg.setdefault(section, {})[key] = val
    return cfg


def _write_config_toml(cfg: dict) -> None:
    path = _streamlit_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for section in ("theme", "client"):
        if section not in cfg or not cfg[section]:
            continue
        lines.append(f"[{section}]")
        for k, v in cfg[section].items():
            lines.append(f'{k} = "{v}"')
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _read_prefs() -> dict:
    try:
        if PREFS_PATH.exists():
            data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_prefs(**kv: Any) -> None:
    data = _read_prefs()
    data.update({k: v for k, v in kv.items() if v is not None})
    PREFS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _saved_theme_key() -> str:
    key = str(_read_prefs().get("theme_key", ""))
    return key if key in STREAMLIT_THEMES else ""


def _sync_theme_config(theme_key: str) -> None:
    """主题色写进 config.toml（Streamlit 合法字段），选择记在本地偏好文件里。

    自定义字段不能放进 config.toml，否则 Streamlit 启动时会报 invalid config option。
    """
    if theme_key not in STREAMLIT_THEMES:
        return
    cfg = _read_config_toml()
    cfg.setdefault("client", {})["toolbarMode"] = "minimal"
    cfg["theme"].update(STREAMLIT_THEMES[theme_key])
    _write_config_toml(cfg)
    _write_prefs(theme_key=theme_key)


def _theme_css_vars(theme_key: str) -> str:
    """把主题色注入成 :root 上的 CSS 变量，让本次请求立即生效。"""
    vars_map = GEEK_CSS_VARS[theme_key]
    decls = "\n  ".join(f"--{k}: {v};" for k, v in vars_map.items())
    return f"<style>\n:root {{\n  {decls}\n}}\n</style>"


CYBER_CSS = """
<style>
/* ========= 全局 ========= */
.stApp {
  background:
    radial-gradient(1200px 600px at 85% -10%, var(--geek-app-radial-a), transparent 60%),
    radial-gradient(900px 500px at 0% 100%, var(--geek-app-radial-b), transparent 60%),
    var(--geek-bg);
  color: var(--geek-text) !important;
}

/* 核心：主题切换时 Streamlit 会注入 inline style="color:..." 覆盖，
 * 用 !important + 容器级选择器强制所有文本跟随主题变量。
 * 下方 accent 色（cyan/dim/green/pink）规则更具体，能正常覆盖这里。 */
[data-testid="stMarkdownContainer"],
[data-testid="stMarkdownContainer"] *,
[data-testid="stSidebar"] *,
.stMetric, .stMetric *,
.stExpander, .stExpander * {
  color: var(--geek-text) !important;
}

/* ========= 滚动条 ========= */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: var(--geek-scrollbar-thumb);
  border-radius: 6px;
  border: 2px solid transparent;
  background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover { background: var(--geek-cyan-glow); background-clip: padding-box; border: 2px solid transparent; }

/* ========= 侧边栏 ========= */
[data-testid="stSidebar"] {
  background: var(--geek-sidebar-grad);
  border-right: 1px solid var(--geek-line);
}
[data-testid="stSidebar"] * { color: var(--geek-text) !important; }
[data-testid="stSidebar"] section[data-testid="stSidebarNavContainer"] {
  background: transparent !important;
}

/* ========= 标题 / Header ========= */
.geek-title {
  font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
  font-size: 2.2rem;
  font-weight: 800;
  letter-spacing: 3px;
  color: var(--geek-text-strong) !important;
  text-shadow: 0 0 22px var(--geek-title-shadow), 0 0 2px var(--geek-cyan-glow);
  margin-bottom: .2rem;
  position: relative;
  display: inline-block;
}
.geek-title::before, .geek-title::after {
  content: attr(data-text);
  position: absolute; left: 0; top: 0; width: 100%; height: 100%;
  pointer-events: none;
}
.geek-title::before { color: var(--geek-pink) !important; transform: translate(-2px, 0); opacity: .45; mix-blend-mode: screen; animation: glitch-top 3s infinite steps(2, end); }
.geek-title::after  { color: var(--geek-cyan) !important; transform: translate(2px, 0); opacity: .45; mix-blend-mode: screen; animation: glitch-bot 2.6s infinite steps(2, end); }
.geek-title span { color: var(--geek-cyan) !important; }
@keyframes glitch-top { 0%,90%,100%{clip-path:inset(0 0 85% 0)} 92%{clip-path:inset(20% 0 55% 0)} 94%{clip-path:inset(60% 0 10% 0)} }
@keyframes glitch-bot { 0%,88%,100%{clip-path:inset(82% 0 0 0)} 90%{clip-path:inset(40% 0 30% 0)} 93%{clip-path:inset(5% 0 70% 0)} }
.geek-sub {
  color: var(--geek-dim) !important;
  font-family: ui-monospace, Consolas, monospace;
  font-size: .78rem;
  letter-spacing: 2px;
  text-transform: uppercase;
}
.geek-hr {
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--geek-cyan) 20%, var(--geek-cyan) 80%, transparent);
  margin: 1rem 0 1.4rem 0;
  position: relative;
}
.geek-hr::after {
  content: ""; position: absolute; left: 0; top: 0; width: 30%; height: 100%;
  background: linear-gradient(90deg, transparent, var(--geek-cyan-glow), transparent);
  animation: hr-sweep 3.2s infinite;
}
@keyframes hr-sweep { 0%{transform:translateX(-100%)} 100%{transform:translateX(450%)} }

/* ========= 排版 ========= */
h1, h2, h3, h4 { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; letter-spacing: .3px; color: var(--geek-text-strong) !important; }
p, li, div { font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }
code, pre, kbd { font-family: ui-monospace, "Cascadia Code", Consolas, monospace !important; }
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li,
[data-testid="stMarkdownContainer"] td, [data-testid="stMarkdownContainer"] strong,
[data-testid="stMarkdownContainer"] span, [data-testid="stMarkdownContainer"] div { color: var(--geek-text); }
[data-testid="stMarkdownContainer"] small, [data-testid="stMarkdownContainer"] em,
label, .stMetricLabel, .stCaption, .stMarkdown p small { color: var(--geek-dim) !important; }
[data-testid="stHeader"] { background: transparent; }
a { color: var(--geek-cyan) !important; text-decoration: none; position: relative; }
a:hover { color: var(--geek-text-strong) !important; }
a::after { content:""; position:absolute; left:0; bottom:-2px; width:0; height:1px; background:var(--geek-cyan); transition: width var(--geek-transition); }
a:hover::after { width:100%; }

/* ========= 卡片（记忆条目） ========= */
.geek-card {
  border: 1px solid var(--geek-glass-border);
  border-radius: 14px;
  padding: 1.1rem 1.3rem .9rem 1.3rem;
  margin-bottom: 1rem;
  background: var(--geek-glass);
  backdrop-filter: blur(10px);
  box-shadow: var(--geek-card-shadow);
  transition: transform var(--geek-transition), box-shadow var(--geek-transition), border-color var(--geek-transition);
  position: relative;
  overflow: hidden;
}
.geek-card::before {
  content: ""; position: absolute; top: 0; left: 0; width: 3px; height: 100%;
  background: linear-gradient(180deg, var(--geek-cyan), transparent 70%);
  opacity: .8;
}
.geek-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--geek-card-shadow-hover);
  border-color: var(--geek-cyan-glow);
}

/* ========= Metric 三栏 ========= */
[data-testid="stMetric"] {
  background: var(--geek-glass);
  border: 1px solid var(--geek-glass-border);
  border-radius: 12px;
  padding: .9rem 1.1rem;
  box-shadow: var(--geek-card-shadow);
  transition: transform var(--geek-transition), box-shadow var(--geek-transition);
}
[data-testid="stMetric"]:hover { transform: translateY(-1px); box-shadow: var(--geek-card-shadow-hover); }
[data-testid="stMetricValue"] { color: var(--geek-cyan) !important; font-family: ui-monospace, Consolas, monospace; font-weight: 700; }
[data-testid="stMetricLabel"] { color: var(--geek-dim) !important; font-size: .75rem; letter-spacing: 1px; text-transform: uppercase; }

/* ========= 按钮 ========= */
.stButton > button, .stDownloadButton > button {
  font-family: ui-monospace, Consolas, monospace;
  border-radius: 10px !important;
  border: 1px solid var(--geek-cyan-soft) !important;
  background: var(--geek-panel-soft) !important;
  color: var(--geek-text) !important;
  position: relative;
  overflow: hidden;
  transition: all var(--geek-transition);
}
.stButton > button:hover, .stDownloadButton > button:hover {
  border-color: var(--geek-cyan) !important;
  color: var(--geek-text-strong) !important;
  box-shadow: 0 0 12px var(--geek-cyan-glow), inset 0 0 12px rgba(34,211,238,.08) !important;
  transform: translateY(-1px);
}
.stButton > button:active, .stDownloadButton > button:active { transform: translateY(0); }
button[kind="primary"] {
  border-radius: 10px !important;
  box-shadow: 0 0 0 rgba(0,0,0,0);
  transition: all var(--geek-transition);
}
button[kind="primary"]:hover {
  box-shadow: 0 0 18px var(--geek-cyan-glow), 0 0 40px var(--geek-cyan-soft);
  transform: translateY(-1px);
}

/* 删除按钮：pink 高亮 */
button[key*="del_"] { border-color: rgba(255,61,129,.35) !important; color: var(--geek-pink) !important; }
button[key*="del_"]:hover {
  border-color: var(--geek-pink) !important;
  box-shadow: 0 0 12px rgba(255,61,129,.35) !important;
}

/* ========= 输入框 ========= */
.stTextInput input, .stTextArea textarea, .stSelectbox [role="combobox"] {
  background: var(--geek-input-bg) !important;
  color: var(--geek-text) !important;
  border: 1px solid var(--geek-line) !important;
  border-radius: 10px !important;
  font-family: ui-monospace, Consolas, monospace !important;
  transition: all var(--geek-transition);
}
.stTextInput input:focus, .stTextArea textarea:focus {
  border-color: var(--geek-cyan) !important;
  box-shadow: 0 0 0 2px var(--geek-cyan-soft), 0 0 12px var(--geek-cyan-glow) !important;
}
.stTextInput input::placeholder, .stTextArea textarea::placeholder { color: var(--geek-dim) !important; }
[data-testid="stTextInput"] label, [data-testid="stTextArea"] label { color: var(--geek-dim) !important; letter-spacing: .5px; font-size: .82rem; }

/* ========= Expander ========= */
div[data-testid="stExpander"] {
  border: 1px solid var(--geek-glass-border) !important;
  border-radius: 10px !important;
  background: var(--geek-glass) !important;
  backdrop-filter: blur(6px);
}
div[data-testid="stExpander"] summary:hover { color: var(--geek-cyan) !important; }
div[data-testid="stExpander"] > div:not([data-testid="stStickyFooter"]) > div { background: transparent !important; color: var(--geek-text) !important; }

/* ========= Badge ========= */
.geek-badge {
  display: inline-block; padding: .14rem .55rem; margin-right: .4rem; margin-bottom: .15rem;
  border: 1px solid var(--geek-cyan-soft);
  border-radius: 999px;
  color: var(--geek-cyan) !important;
  font-size: .72rem;
  font-family: ui-monospace, Consolas, monospace;
  letter-spacing: .5px;
  background: var(--geek-panel-soft);
}
.geek-badge.pink { border-color: rgba(225,29,125,.4); color: var(--geek-pink) !important; }
.geek-badge.green { border-color: rgba(16,185,129,.35); color: var(--geek-green) !important; }

/* ========= Checkbox / Radio / Toggle ========= */
.stCheckbox label, .stRadio label, .stMarkdown label { color: var(--geek-text) !important; }
[data-testid="stCheckbox"] label, [data-testid="stRadio"] label, [data-testid="stSidebar"] label { color: var(--geek-text) !important; }

/* ========= Code ========= */
.stCode pre, pre { background: var(--geek-input-bg) !important; border-radius: 8px; }
div[data-testid="stCode"] { border: 1px solid var(--geek-line) !important; border-radius: 8px; }

/* ========= Selectbox ========= */
/* Streamlit selectbox 有三层：容器 div / combobox / 下拉 listbox，统一覆盖 */
[data-testid="stSelectbox"],
[data-testid="stSelectbox"] > div,
[data-testid="stSelectbox"] [role="combobox"],
[data-testid="stSelectbox"] [role="listbox"],
[data-testid="stMultiSelect"],
[data-testid="stMultiSelect"] > div,
[data-testid="stMultiSelect"] [role="combobox"],
[data-testid="stMultiSelect"] [role="listbox"] {
  background: var(--geek-input-bg) !important;
  color: var(--geek-text) !important;
  border: 1px solid var(--geek-line) !important;
  border-radius: 8px !important;
  box-shadow: none !important;
}
[data-testid="stSelectbox"] [role="combobox"]:hover,
[data-testid="stMultiSelect"] [role="combobox"]:hover {
  border-color: var(--geek-cyan-soft) !important;
}
[data-testid="stSelectbox"] [role="combobox"]:focus-within,
[data-testid="stMultiSelect"] [role="combobox"]:focus-within {
  border-color: var(--geek-cyan) !important;
  box-shadow: 0 0 0 2px var(--geek-cyan-soft), 0 0 10px var(--geek-cyan-glow) !important;
}
/* 下拉选项 */
[data-testid="stSelectbox"] li[role="option"],
[data-testid="stMultiSelect"] li[role="option"] {
  background: var(--geek-input-bg) !important;
  color: var(--geek-text) !important;
  border: none !important;
}
[data-testid="stSelectbox"] li[aria-selected="true"],
[data-testid="stMultiSelect"] li[aria-selected="true"] {
  background: var(--geek-cyan-soft) !important;
  color: var(--geek-text-strong) !important;
}

/* ========= Tabs ========= */
[data-testid="stTabs"], button[data-baseweb="tab"], button[data-baseweb="tab-list"], button[aria-selected="false"] { color: var(--geek-text) !important; }
button[aria-selected="true"] { color: var(--geek-cyan) !important; border-bottom: 2px solid var(--geek-cyan) !important; }

/* ========= Caption / Helper ========= */
.stCaption, .stHelper, small, em { color: var(--geek-dim) !important; }

/* ========= Callout / Alert（info / success / warning / error）========= */
/* Streamlit 四种 callout 有硬编码 inline 颜色，主题切换不跟随。手动覆盖。 */
[data-testid="stAlertContainer"],
[data-testid="stInfoContainer"],
[data-testid="stSuccessContainer"],
[data-testid="stWarningContainer"],
[data-testid="stErrorContainer"],
div[role="alert"], div[role="status"] {
  color: var(--geek-text) !important;
  background: var(--geek-glass) !important;
  border-radius: 10px !important;
  box-shadow: 0 0 0 1px var(--geek-glass-border) !important;
}
/* 四种类型各自的左侧色条 */
[data-testid="stInfoContainer"] { border-left: 3px solid var(--geek-cyan) !important; }
[data-testid="stSuccessContainer"] { border-left: 3px solid var(--geek-green) !important; }
[data-testid="stWarningContainer"] { border-left: 3px solid #f59e0b !important; }
[data-testid="stErrorContainer"] { border-left: 3px solid var(--geek-pink) !important; }
/* callout 内部文字统一 */
[data-testid="stAlertContainer"] *,
[data-testid="stInfoContainer"] *,
[data-testid="stSuccessContainer"] *,
[data-testid="stWarningContainer"] *,
[data-testid="stErrorContainer"] * { color: var(--geek-text) !important; }

/* ========= 卡片容器 / geek-card 旧类兼容 ========= */
.geek-card h4 { margin: 0 0 .3rem 0; color: var(--geek-text-strong) !important; }
.geek-summary {
  margin: .1rem 0 .7rem 0;
  padding: .6rem .85rem;
  border-left: 3px solid var(--geek-cyan);
  border-radius: 6px;
  background: var(--geek-glass);
  color: var(--geek-text) !important;
  font-size: .92rem;
  line-height: 1.65;
}
.geek-summary b { color: var(--geek-cyan) !important; }
.geek-meta { color: var(--geek-dim) !important; font-size: .78rem; font-family: ui-monospace, Consolas, monospace; }
</style>
"""


def _render_header() -> None:
    st.markdown(
        '<div class="geek-title" data-text="DeepShare">Deep<span>Share</span></div>'
        '<div class="geek-sub">DeepSeek Share → Local Memory Vault</div>'
        '<div class="geek-hr"></div>',
        unsafe_allow_html=True,
    )


def _result_card(res: ScrapeResult) -> None:
    if res.ok:
        st.success(f"抓取完成 · 策略：{res.strategy}")
        st.caption(f"已保存：{res.saved_path.name if res.saved_path else '-'} · {len(res.markdown)} 字符")
        with st.expander("预览抓取结果", expanded=False):
            st.markdown(res.markdown)
        st.download_button(
            "下载本次 Markdown",
            data=res.markdown.encode("utf-8"),
            file_name=res.saved_path.name if res.saved_path else "deepseek.md",
            mime="text/markdown",
        )
    else:
        st.error(f"抓取失败：{res.message}")
        st.info(
            "排错建议：\n"
            "1. 打开原始分享链接，确认页面本身能看到对话内容；\n"
            "2. 侧边栏勾选\"允许有头浏览器兜底\"后重试，在弹出的窗口里手动完成验证；\n"
            "3. 保留调试文件后重试，查看 debug_artifacts/ 中的 *_page.html 与 *_network.json；\n"
            "4. 按【排故与填坑手册】用 F12 更新选择器或接口字段。"
        )

    with st.expander(f"抓取日志（{len(res.logs)} 行）", expanded=not res.ok):
        st.code("\n".join(res.logs) or "无日志", language="text")


def render_sidebar() -> ScrapeResult | None:
    with st.sidebar:
        # 主题切换（三选一，即时生效 + 写入 config.toml）
        cur_theme = st.session_state.get("theme_key", "dark")
        THEMES_LABEL = {
            "dark":  "🌙 赛博 Dark",
            "light": "☀️ 清爽 Light",
            "amber": "🟠 琥珀 Amber",
        }
        theme_label = st.selectbox(
            "🎨 主题风格",
            options=list(THEMES_LABEL.values()),
            index=list(THEMES_LABEL.keys()).index(cur_theme),
            key="theme_selector",
            help="选择后即时生效并自动保存",
        )
        new_theme = list(THEMES_LABEL.keys())[list(THEMES_LABEL.values()).index(theme_label)]
        if new_theme != cur_theme:
            st.session_state["theme_key"] = new_theme
            _sync_theme_config(new_theme)
            st.rerun()

        st.markdown("### 抓取控制台")
        st.caption("输入 DeepSeek 分享链接，自动抓取并清洗为 Markdown")
        url = st.text_input(
            "DeepSeek 分享链接",
            placeholder="https://chat.deepseek.com/share/xxxxxxxx",
            key="share_url",
        )
        with st.expander("高级设置", expanded=False):
            dom_wait = st.slider("DOM 等待上限（秒）", 5, 30, 12, 1)
            allow_headed = st.checkbox("允许有头浏览器兜底（可手动过验证）", value=True)
            save_debug = st.checkbox("保留调试文件（HTML / Network JSON）", value=True)

        clicked = st.button("开始抓取", type="primary", width="stretch")

        result: ScrapeResult | None = None
        if clicked:
            if not url.strip():
                st.warning("请先填写分享链接")
            else:
                logs: list[str] = []
                with st.status("正在抓取，请稍候 …", expanded=True) as status:
                    box = st.empty()

                    def cb(line: str) -> None:
                        logs.append(line)
                        box.code("\n".join(logs[-26:]), language="text")

                    result = scrape_deepseek(
                        url,
                        dom_wait=dom_wait,
                        allow_headed=allow_headed,
                        save_debug=save_debug,
                        log=cb,
                    )
                    box.code("\n".join(logs[-26:]), language="text")
                    status.update(
                        label="抓取完成" if result.ok else "抓取失败",
                        state="complete" if result.ok else "error",
                    )
                if result.ok:
                    load_memories.clear()
                    st.rerun()

        st.markdown("---")
        st.markdown("##### 📦 批量操作")

        items = load_memories(_dir_signature())
        if items:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for it in items:
                    zf.writestr(it["name"], it["raw"])
            zip_bytes = buf.getvalue()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.download_button(
                f"⬇️ 导出全部为 ZIP（{len(items)} 条）",
                data=zip_bytes,
                file_name=f"deepshare_backup_{ts}.zip",
                mime="application/zip",
                width="stretch",
                help="打包 my_memories/ 下所有 .md，便于备份和迁移",
            )
        else:
            st.caption("暂无记忆可导出")

        st.markdown("---")
        st.markdown("##### ✨ AI 总结")
        ai_cfg = _read_ai_config()
        with st.expander("接口配置", expanded=not _ai_is_ready(ai_cfg)):
            key_in = st.text_input(
                "API Key",
                value=ai_cfg["api_key"],
                type="password",
                key="ai_key_input",
                help="仅保存在本机 .deepshare_ai.json，不会上传 GitHub",
            )
            base_in = st.text_input(
                "Base URL", value=ai_cfg["base_url"], key="ai_base_input",
                help="默认 https://api.deepseek.com，任何 OpenAI 兼容端点均可",
            )
            model_in = st.text_input(
                "模型", value=ai_cfg["model"], key="ai_model_input",
                help="默认 deepseek-chat",
            )
            if st.button("保存配置", width="stretch", key="ai_save_btn"):
                if not key_in.strip():
                    st.warning("请先填写 API Key")
                else:
                    _write_ai_config({"api_key": key_in, "base_url": base_in, "model": model_in})
                    st.toast("AI 配置已保存", icon="✅")
                    st.rerun()
        if _ai_is_ready(ai_cfg):
            st.caption("✅ 已配置，可在记忆卡片上点击「AI 总结」")
        else:
            st.caption("未配置 API Key，AI 总结不可用")

        st.markdown("---")
        st.markdown("##### 存储位置")
        st.caption(str(MEMORY_DIR))
        st.markdown("##### 环境自检")
        st.caption("Playwright 内核缺失时执行：`python -m playwright install chromium`")

    return result


def _stream_generate_prompt(item: dict, cfg: dict) -> str:
    """流式生成续聊 Prompt：边收边显示，并实时显示已等待/已生成字数。

    用一个后台线程跑网络请求、主线程轮询队列，这样即使首字节还没回来，
    界面上的计时器也在跳，不会看起来像卡死。
    """
    q: "queue.Queue[tuple[str, str]]" = queue.Queue()

    def worker() -> None:
        try:
            for piece in _ai_chat_stream(cfg, _handoff_messages(item)):
                q.put(("chunk", piece))
            q.put(("done", ""))
        except RuntimeError as exc:
            q.put(("error", str(exc)))
        except Exception as exc:  # 兜住线程内的一切异常，否则界面会一直等
            q.put(("error", f"{type(exc).__name__}: {exc}"))

    threading.Thread(target=worker, daemon=True).start()

    box = st.empty()
    status = st.status("正在通读对话 …", expanded=False)
    acc: list[str] = []
    start = time.time()
    while True:
        try:
            kind, value = q.get(timeout=1)
        except queue.Empty:
            elapsed = int(time.time() - start)
            status.update(label=f"等待接口响应 … 已 {elapsed} 秒")
            continue
        if kind == "chunk":
            acc.append(value)
            text = "".join(acc)
            box.markdown(text[-2000:] + "▍")
            status.update(label=f"生成中 … 已 {int(time.time() - start)} 秒 / {len(text)} 字")
        elif kind == "error":
            status.update(label="生成失败", state="error")
            box.empty()
            st.error(f"生成失败：{value}")
            return ""
        else:
            break

    status.update(
        label=f"完成，用时 {int(time.time() - start)} 秒", state="complete", expanded=False
    )
    box.markdown("已写入卡片，可在下方查看与复制。")
    text = _clean_prompt("".join(acc))
    if not text:
        st.error("接口返回了空内容，请重试")
        return ""
    return text


def render_main(result: ScrapeResult | None) -> None:
    _render_header()

    # —— 删除（两阶段：按钮点击只设标记，rerun 后真正执行）——
    pending = st.session_state.pop("_pending_delete", None)
    if pending:
        try:
            Path(pending).unlink(missing_ok=True)
        except OSError:
            pass
        load_memories.clear()
        st.toast("🗑 记忆已删除", icon="✅")
        st.rerun()

    items = load_memories(_dir_signature())

    if result is not None:
        _result_card(result)

    total_chars = sum(i["chars"] for i in items)
    total_size = sum(i["size"] for i in items)
    c1, c2, c3 = st.columns(3)
    c1.metric("记忆条数", f"{len(items)}")
    c2.metric("累计字符", f"{total_chars:,}")
    c3.metric("占用空间", f"{total_size / 1024:.1f} KB")

    st.markdown("---")

    query = st.text_input(
        "关键词搜索",
        placeholder="搜索标题、正文、代码片段 …（留空显示全部）",
        key="search_query",
    ).strip().lower()

    filtered = items
    if query:
        filtered = [
            i for i in items
            if query in i["title"].lower() or query in i["body"].lower() or query in i["name"].lower()
        ]
        st.caption(f"命中 {len(filtered)} / {len(items)} 条")

    if not items:
        st.info("记忆库还是空的。在左侧粘贴 DeepSeek 分享链接并点击「开始抓取」即可。")
        return
    if not filtered:
        st.warning(f"没有匹配「{query}」的记忆。")
        return

    shown = st.session_state.get("visible_count", 6)
    if query:
        shown = len(filtered)  # 搜索时全部展开，方便定位

    for item in filtered[:shown]:
        st.markdown('<div class="geek-card">', unsafe_allow_html=True)
        head_l, head_r = st.columns([5, 2])
        with head_l:
            st.markdown(f"#### {item['title']}")
        with head_r:
            st.caption(f"{item['mtime']}")
        badges = (
            f'<span class="geek-badge green">{item["chars"]:,} 字符</span>'
            f'<span class="geek-badge">{item["strategy"] or "未知策略"}</span>'
        )
        st.markdown(badges, unsafe_allow_html=True)
        if item.get("summary"):
            st.markdown(
                f'<div class="geek-summary">✨ <b>AI 总结</b><br>{html.escape(item["summary"])}</div>',
                unsafe_allow_html=True,
            )
        if item["source"]:
            st.caption(f"来源：{item['source']}")
            st.markdown(f"[打开原始分享链接]({item['source']})")
        preview = item["body"][:420]
        if len(item["body"]) > 420:
            preview += "\n\n…"
        st.markdown(preview)
        with st.expander("展开全文", expanded=False):
            st.markdown(item["body"])
        with st.expander("查看元数据 / 文件名", expanded=False):
            st.json(
                {
                    "文件": item["name"],
                    "标题": item["title"],
                    "来源": item["source"],
                    "抓取时间": item["scraped_at"],
                    "抓取策略": item["strategy"],
                    "字符数": item["chars"],
                    "字节数": item["size"],
                }
            )
        saved_prompt = _read_prompt(item)
        if saved_prompt:
            opened = st.session_state.get("_prompt_open") == item["name"]
            with st.expander("📋 续聊 Prompt（右上角一键复制）", expanded=opened):
                st.code(saved_prompt, language="text")
        act_a, act_b, act_c = st.columns([1, 1, 1])
        with act_a:
            has_summary = bool(item.get("summary"))
            if st.button(
                "🔄 重新总结" if has_summary else "✨ AI 总结",
                key=f"sum_{item['path']}",
                width="stretch",
            ):
                cfg = _read_ai_config()
                if not _ai_is_ready(cfg):
                    st.warning("请先在左侧「✨ AI 总结 → 接口配置」里填写 API Key")
                else:
                    with st.spinner("正在生成总结 …"):
                        try:
                            summary = summarize_memory(item, cfg)
                            _set_summary(Path(item["path"]), summary)
                        except RuntimeError as exc:
                            st.error(f"总结失败：{exc}")
                        else:
                            load_memories.clear()
                            st.toast("✨ 总结已生成", icon="✅")
                            st.rerun()
        with act_b:
            if st.button(
                "🔄 重做 Prompt" if saved_prompt else "📋 续聊 Prompt",
                key=f"prompt_{item['path']}",
                width="stretch",
                help="读完这段对话，生成一段能粘到新对话窗口继续干的提示词",
            ):
                cfg = _read_ai_config()
                if not _ai_is_ready(cfg):
                    st.warning("请先在左侧「✨ AI 总结 → 接口配置」里填写 API Key")
                else:
                    prompt_text = _stream_generate_prompt(item, cfg)
                    if prompt_text:
                        _write_prompt(item, prompt_text)
                        st.session_state["_prompt_open"] = item["name"]
                        load_memories.clear()
                        st.rerun()
        with act_c:
            if st.button("🗑 删除这条记忆", key=f"del_{item['path']}", width="stretch"):
                st.session_state["_pending_delete"] = item["path"]
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    if not query and shown < len(filtered):
        if st.button(f"加载更多（还有 {len(filtered) - shown} 条）", width="stretch"):
            st.session_state["visible_count"] = shown + 6
            st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="DeepShare · DeepSeek 分享记忆库",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # 主题初始化：优先读本地偏好文件，fallback 到 config.toml 的 base 推断
    if "theme_key" not in st.session_state:
        cfg = _read_config_toml()
        saved = _saved_theme_key()
        base = (cfg.get("theme", {}).get("base") or "dark").lower()
        if saved in STREAMLIT_THEMES:
            st.session_state["theme_key"] = saved
        else:
            st.session_state["theme_key"] = "light" if base.startswith("light") else "dark"
        _sync_theme_config(st.session_state["theme_key"])

    st.markdown(_theme_css_vars(st.session_state["theme_key"]), unsafe_allow_html=True)
    st.markdown(CYBER_CSS, unsafe_allow_html=True)
    _ensure_dirs()
    st.session_state.setdefault("visible_count", 6)

    result = render_sidebar()
    render_main(result)


if __name__ == "__main__":
    main()
