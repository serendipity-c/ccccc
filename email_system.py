#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI驱动邮件发送系统 - 完整版
- 使用 wbsu2003/stock-scanner-mcp 作为主要行情与 AI 源（通过 HTTP 接口）
- 当 stock-scanner-mcp 不可用时回退到智谱 zhipuai（若配置）
- Supabase 用于读取用户与自选股（user_watchlist 可能只有 name 无 code 的场景）
- 支持从 name 中提取/解析股票 code 与 market（A/HK/US）
- 可通过环境变量 STOCK_SCANNER_URL 覆盖 stock-scanner-mcp 地址

注意：为便于测试与 CI，我保留了一些敏感值的硬编码示例（按你的要求）。在生产环境中强烈建议把它们移到 Secrets / 环境变量。
"""

from __future__ import annotations

import os
import re
import sys
import time
import smtplib
import requests
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from requests.exceptions import ConnectionError as RequestsConnectionError

# -------------------- 全部硬编码配置（按你的要求） --------------------
# SMTP (Resend 示例)
RESEND_API_KEY = "re_Nm5shWrw_4Xp8c94P9VFQ12SC7BxEuuv7"
SMTP_HOST = "smtp.resend.com"
SMTP_PORT = 587
SMTP_USER = "resend"
FROM_NAME = "Portfolio Guardian"
FROM_EMAIL = "noreply@chenzhaoqi.asia"

# Supabase (数据库)
SUPABASE_URL = "https://ayjxvejaztusajdntbkh.supabase.co"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF5anh2ZWphenR1c2FqZG50YmtoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQ0ODAxMSwiZXhwIjoyMDg0MDI0MDExfQ.2Ebe2Ft1gPEfyem0Qie9fGaQ8P3uhJvydGBFyCkvIgE"

# 智谱AI (回退)
ZHIPUAI_API_KEY = "21f9ca7cfa0d44f4afeed5ed9d083b23.4zxzk7cZBhr0wnz7"

# stock-scanner-mcp 服务地址（优先使用）
STOCK_SCANNER_URL = os.environ.get("STOCK_SCANNER_URL", "http://localhost:8000").rstrip("/")

# -------------------- 日志配置 --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------- 惰性导入状态 --------------------
_ZHIPUAI_CLS = None
_logged_missing = set()

# -------------------- 帮助：导入 zhipuai --------------------
def _import_zhipuai_class():
    global _ZHIPUAI_CLS, _logged_missing
    if _ZHIPUAI_CLS is None:
        try:
            from zhipuai import ZhipuAI  # type: ignore
            _ZHIPUAI_CLS = ZhipuAI
            logger.info("zhipuai SDK 已导入")
        except ImportError:
            _ZHIPUAI_CLS = False
            if "zhipuai" not in _logged_missing:
                logger.warning("zhipuai 未安装 — AI 回退将不可用。")
                _logged_missing.add("zhipuai")
        except Exception as e:
            _ZHIPUAI_CLS = False
            logger.warning(f"导入 zhipuai 时出错（已降级）：{e}")
    return _ZHIPUAI_CLS if _ZHIPUAI_CLS else None

def get_zhipu_client():
    if not ZHIPUAI_API_KEY:
        logger.warning("未设置 ZHIPUAI_API_KEY；zhipuai 不可用。")
        return None
    cls = _import_zhipuai_class()
    if not cls:
        return None
    try:
        return cls(api_key=ZHIPUAI_API_KEY)
    except Exception as e:
        logger.error(f"初始化 zhipuai 客户端失败: {e}")
        return None

def _call_zhipu(prompt: str) -> str | None:
    client = get_zhipu_client()
    if not client:
        return None
    try:
        response = client.chat.completions.create(
            model="glm-4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=2000
        )
    except Exception:
        try:
            response = client.create(prompt=prompt)
        except Exception as e:
            logger.error(f"调用 zhipuai 失败: {e}")
            return None

    try:
        if hasattr(response, "choices"):
            return getattr(response.choices[0].message, "content", None) or getattr(response.choices[0], "text", None)
        if isinstance(response, dict):
            choices = response.get("choices") or []
            if choices:
                first = choices[0]
                return (first.get("message") or {}).get("content") or first.get("text") or None
        return str(response)
    except Exception:
        return None

# -------------------- stock-scanner-mcp HTTP 客户端 --------------------
def _call_stock_scanner(path: str, params: dict | None = None, timeout: int = 15) -> dict | str | None:
    """
    GET 调用 stock-scanner-mcp 并返回 JSON（优先）或文本
    path: 以 '/' 开头的路径，如 '/stock_ai_analysis'
    """
    base = STOCK_SCANNER_URL
    if not base:
        logger.warning("STOCK_SCANNER_URL 未配置，无法调用 stock-scanner-mcp")
        return None
    url = f"{base}{path}"
    try:
        logger.debug(f"GET {url} params={params}")
        resp = requests.get(url, params=params or {}, timeout=timeout)
        if resp.status_code != 200:
            logger.warning(f"stock-scanner-mcp {url} 返回 {resp.status_code}: {resp.text[:200]}")
            return None
        try:
            return resp.json()
        except Exception:
            return resp.text
    except RequestsConnectionError as e:
        logger.warning(f"连接到 stock-scanner-mcp ({url}) 失败: {e}")
        return None
    except Exception as e:
        logger.debug(f"调用 stock-scanner-mcp 时异常: {e}")
        return None

# -------------------- 名称到代码解析器（支持只有 name 情况） --------------------
def infer_market_and_format(raw_code: str) -> tuple[str, str]:
    """
    推断 market_type ('A','HK','US') 并格式化 code:
    - '600519' -> ('600519','A')
    - 'sh600519' -> ('600519','A')
    - '0700' or '700' -> ('0700','HK') or ('700','HK') (示例)
    - 'AAPL' -> ('AAPL','US')
    """
    if not raw_code:
        return ("", "A")
    s = str(raw_code).strip()
    s_low = s.lower()
    s_clean = re.sub(r'[\s\-_\.]', '', s_low)

    # sh/sz 前缀
    m = re.match(r'^(sh|sz)(0*\d+)$', s_clean)
    if m:
        return (m.group(2).lstrip("0") or m.group(2), "A")
    # hk 前缀
    m = re.match(r'^(hk)(0*\d+)$', s_clean)
    if m:
        return (m.group(2).lstrip("0") or m.group(2), "HK")
    # 6 位数字 -> A
    if re.fullmatch(r'\d{6}', s_clean):
        return (s_clean.lstrip("0") or s_clean, "A")
    # 4-5 位数字 -> HK
    if re.fullmatch(r'\d{4,5}', s_clean):
        return (s_clean.lstrip("0") or s_clean, "HK")
    # 后缀 hk
    m = re.match(r'^(\d{1,6})hk$', s_clean)
    if m:
        return (m.group(1).lstrip("0") or m.group(1), "HK")
    # us 或 gb_ 前缀 -> US
    if s_clean.startswith("us"):
        return (s_clean[2:].upper(), "US")
    if s_clean.startswith("gb"):
        return (s_clean[2:].upper(), "US")
    # 纯字母 -> US ticker
    if re.fullmatch(r'[a-zA-Z]{1,6}', s_clean):
        return (s_clean.upper(), "US")
    # 字母前缀
    m = re.match(r'^([A-Za-z]+)', s_clean)
    if m:
        return (m.group(1).upper(), "US")
    # 提取数字回退
    digits = re.sub(r'\D', '', s_clean)
    if digits:
        if len(digits) == 6:
            return (digits, "A")
        if len(digits) in (4,5):
            return (digits, "HK")
        return (digits, "A")
    return (s_clean.upper(), "US")

def _extract_code_from_name(text: str) -> str | None:
    if not text:
        return None
    t = text.strip()
    m = re.search(r'[\(\（\[]\s*([0-9A-Za-z]{1,6})\s*[\)\）\]]', t)
    if m:
        return m.group(1)
    m = re.search(r'([0-9A-Za-z]{4,6})(?:\s*$)', t)
    if m:
        return m.group(1)
    m = re.search(r'([0-9]{4,6})\s*[.\-/_]\s*(sh|sz|hk|us)?', t, flags=re.I)
    if m:
        return m.group(1)
    m = re.search(r'\b([A-Za-z]{1,6})\b', t)
    if m and not re.search(r'\d', m.group(1)):
        return m.group(1)
    return None

def _try_stock_scanner_search(name: str) -> dict | None:
    """
    尝试调用 stock-scanner-mcp 的常见搜索端点以解析 name -> code。
    若你确切知道搜索端点，请替换 endpoints 列表为实际路径以提高准确性。
    """
    if not name or not STOCK_SCANNER_URL:
        return None
    endpoints = [
        "/search_stock", "/stock_search", "/search", "/stock_lookup",
        "/stock_info", "/suggest", "/mcp/search", "/api/search",
    ]
    params_variants = [{"q": name}, {"query": name}, {"keyword": name}, {"stock_name": name}, {"name": name}]
    headers = {"Accept": "application/json"}
    for ep in endpoints:
        url = f"{STOCK_SCANNER_URL.rstrip('/')}{ep}"
        for params in params_variants:
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=6)
                if resp.status_code != 200:
                    continue
                try:
                    j = resp.json()
                except Exception:
                    continue
                candidates = []
                if isinstance(j, list):
                    candidates = j
                elif isinstance(j, dict):
                    for k in ("data", "results", "items"):
                        if k in j and isinstance(j[k], list):
                            candidates = j[k]
                            break
                    if not candidates:
                        candidates = [j]
                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    for key in ("code", "stock_code", "symbol", "ticker", "id"):
                        if key in item and item[key]:
                            return item
                    for v in item.values():
                        if isinstance(v, str) and re.fullmatch(r'\d{4,6}', v):
                            return item
            except Exception:
                continue
    return None

def resolve_code_by_name(name: str) -> tuple[str, str] | tuple[None, None]:
    if not name:
        return (None, None)
    direct = _extract_code_from_name(name)
    if direct:
        code, market = infer_market_and_format(direct)
        if code:
            return (code, market)
    try:
        res = _try_stock_scanner_search(name)
        if res:
            for key in ("code", "stock_code", "symbol", "ticker", "id"):
                if key in res and res[key]:
                    c, m = infer_market_and_format(str(res[key]))
                    if c:
                        return (c, m)
            for v in res.values():
                if isinstance(v, dict):
                    for key in ("code", "stock_code", "symbol", "ticker", "id"):
                        if key in v and v[key]:
                            c, m = infer_market_and_format(str(v[key]))
                            if c:
                                return (c, m)
    except Exception:
        pass
    fallback = _extract_code_from_name(name)
    if fallback:
        c, m = infer_market_and_format(fallback)
        if c:
            return (c, m)
    letters = re.findall(r'[A-Za-z]{1,6}', name)
    if letters:
        c, m = infer_market_and_format(letters[0])
        if c:
            return (c, m)
    return (None, None)

# -------------------- Supabase 帮助函数 --------------------
def _supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json"
    }

def get_user_id_by_email(email: str) -> str | None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.warning("SUPABASE_URL 或 SUPABASE_SERVICE_KEY 未设置；无法解析 user_id。")
        return None
    headers = _supabase_headers()
    candidate_tables = ["users", "user_profiles", "profiles"]
    for table in candidate_tables:
        url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
        params = {"select": "id,user_id,email", "email": f"eq.{email}"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
        except Exception as e:
            logger.debug(f"请求 {url} 失败: {e}")
            continue
        if resp.status_code == 200:
            try:
                rows = resp.json()
            except Exception:
                continue
            if not rows:
                continue
            first = rows[0]
            uid = first.get("user_id") or first.get("id")
            if uid:
                logger.info(f"通过表 {table} 找到 user_id={uid} for email={email}")
                return str(uid)
            for v in first.values():
                if v:
                    logger.info(f"通过表 {table} 找到可能的 user_id 值={v} for email={email}")
                    return str(v)
        elif resp.status_code == 404:
            logger.debug(f"表 {table} 不存在 (404)，跳过")
            continue
        else:
            logger.debug(f"查询 {table} 返回 {resp.status_code}: {resp.text}")
    logger.warning(f"未能通过常见表解析 email={email} 对应的 user_id")
    return None

def get_users_with_email_enabled(report_type: str = "morning_brief") -> list[dict]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("SUPABASE_URL 或 SUPABASE_SERVICE_KEY 未设置；无法查询用户列表。")
        return []
    headers = _supabase_headers()
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_email_preferences"
    params = {"select": "*", "enabled": "eq.true", f"{report_type}->>enabled": "eq.true"}
    logger.info(f"查询启用了 {report_type} 的用户...")
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
    except Exception as e:
        logger.error(f"请求 Supabase 失败: {e}")
        return []
    if resp.status_code != 200:
        logger.error(f"查询 user_email_preferences 失败: {resp.status_code} - {resp.text}")
        return []
    try:
        records = resp.json()
    except Exception as e:
        logger.error(f"解析 Supabase 响应失败: {e}")
        return []
    enhanced = []
    for rec in records:
        email = rec.get("email") or rec.get("contact") or ""
        user_id = rec.get("user_id") or None
        if not user_id and email:
            user_id = get_user_id_by_email(email)
        rec["resolved_user_id"] = user_id or ""
        enhanced.append(rec)
    return enhanced

def get_user_watchlist(user_id: str) -> list[dict]:
    """
    从 Supabase 获取用户自选股；如果只有 name 则解析 code 与 market。
    返回每条记录：{"name","raw_code","code","market"}
    """
    if not user_id:
        return []
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("SUPABASE_URL 或 SUPABASE_SERVICE_KEY 未设置；无法查询自选股。")
        return []
    headers = _supabase_headers()
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_watchlist"
    params = {"select": "name,code", "user_id": f"eq.{user_id}"}
    logger.info(f"请求 Supabase: GET {url} params={params}")
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
    except Exception as e:
        logger.error(f"请求 user_watchlist 失败: {e}")
        return []
    if resp.status_code != 200:
        logger.error(f"查询自选股失败: {resp.status_code} - {resp.text}")
        return []
    try:
        rows = resp.json()
    except Exception as e:
        logger.error(f"解析自选股响应失败: {e}")
        return []
    normalized = []
    for row in rows:
        name = row.get("name") or row.get("stock_name") or ""
        raw_code = row.get("code") or row.get("symbol") or ""
        name = str(name).strip() if name is not None else ""
        raw_code = str(raw_code).strip() if raw_code is not None else ""
        if raw_code:
            formatted_code, market = infer_market_and_format(raw_code)
        else:
            formatted_code, market = resolve_code_by_name(name)
        normalized.append({
            "name": name or formatted_code or raw_code,
            "raw_code": raw_code,
            "code": formatted_code or "",
            "market": market or "A"
        })
    logger.info(f"   用户 {user_id} 有 {len(normalized)} 条自选股（含格式化 code 与 market）")
    return normalized

# -------------------- 使用 stock-scanner-mcp 的行情/AI 封装 --------------------
def get_stock_quote(stock_code: str, market_type: str = "A") -> dict | None:
    if not stock_code:
        return None
    params = {"stock_code": stock_code, "market_type": market_type}
    res = _call_stock_scanner("/stock_price", params)
    if not res:
        return None
    if isinstance(res, dict):
        src = res["data"] if "data" in res and isinstance(res["data"], dict) else res
        mapped = {
            "code": src.get("code") or stock_code,
            "name": src.get("name") or src.get("stock_name") or "",
            "price": src.get("price") or src.get("now") or src.get("close") or 0,
            "change": src.get("change") or src.get("chg") or src.get("percent") or 0,
            "volume": src.get("volume") or src.get("成交量") or 0,
            "amount": src.get("amount") or 0,
            "high": src.get("high") or src.get("highest") or 0,
            "low": src.get("low") or src.get("lowest") or 0,
            "open": src.get("open") or 0,
            "yesterday_close": src.get("pre_close") or src.get("yesterday_close") or 0,
        }
        return mapped
    return None

def get_market_index() -> dict:
    indices_codes = {"sh": ("000001", "A"), "sz": ("399001", "A"), "cyb": ("399006", "A")}
    out = {}
    for k, (code, mt) in indices_codes.items():
        candidates = [code, f"sh{code}", f"sz{code}"]
        quote = None
        for c in candidates:
            q = get_stock_quote(c, mt)
            if q:
                quote = q
                break
        if quote:
            out[k] = {"name": quote.get("name") or k, "code": code, "price": quote.get("price"), "change": quote.get("change")}
    return out

def get_ai_analysis_for_stock(stock_code: str, market_type: str = "A") -> str | None:
    if not stock_code:
        return None
    params = {"stock_code": stock_code, "market_type": market_type}
    res = _call_stock_scanner("/stock_ai_analysis", params, timeout=40)
    if not res:
        return None
    if isinstance(res, dict):
        for key in ("ai_analysis", "ai", "content", "html", "data", "result"):
            if key in res and res[key]:
                if isinstance(res[key], dict) and "content" in res[key]:
                    return res[key]["content"]
                return res[key]
        return str(res)
    return str(res)

# -------------------- AI 内容生成（优先 stock-scanner-mcp，再 zhipuai 回退） --------------------
def generate_ai_content_for_watchlist(watchlist: list) -> str:
    parts = []
    for s in (watchlist or [])[:8]:
        code = s.get("code", "")
        market = s.get("market", "A") or "A"
        name = s.get("name") or code or s.get("raw_code") or "未知"
        if not code:
            parts.append(f"<p><strong>{name}</strong>：无代码，无法获取 AI 分析。</p>")
            continue
        ai_text = get_ai_analysis_for_stock(code, market)
        if ai_text:
            parts.append(f"<h3>{name} ({code} - {market})</h3><div>{ai_text}</div>")
        else:
            prompt = f"请对股票 {name} ({code}, 市场 {market}) 做简短分析，包含趋势与操作建议（中文，约100字）。"
            z = _call_zhipu(prompt)
            if z:
                parts.append(f"<h3>{name} ({code} - {market})</h3><div>{z}</div>")
            else:
                parts.append(f"<p><strong>{name} ({code})</strong>：无法获取 AI 分析，使用回退简述。</p>")
    if not parts:
        return "<p>暂无可用自选股分析。</p>"
    return "\n".join(parts)

# -------------------- 报告生成 --------------------
def generate_morning_brief_ai(user_id: str, watchlist: list) -> str:
    logger.info(f"为用户 {str(user_id)[:12]}... 生成早市简报")
    try:
        indices = get_market_index()
        stock_context = generate_ai_content_for_watchlist(watchlist)
        header = "<p>以下内容来自 stock-scanner-mcp 的 AI 分析模块（按自选股汇总）。</p>"
        indices_html = ""
        if indices:
            indices_html += "<ul>"
            for k, idx in indices.items():
                try:
                    change = float(idx.get("change") or 0)
                except Exception:
                    change = 0
                indices_html += f"<li>{idx.get('name')}: {idx.get('price')} ({('+' if change>0 else '')}{change})</li>"
            indices_html += "</ul>"
        content = f"""
        <h2>早市快讯</h2>
        {header}
        <h3>市场要点</h3>
        {indices_html}
        <h3>自选股深度分析</h3>
        {stock_context}
        <p>提示：以上 AI 分析来自 stock-scanner-mcp 的 /stock_ai_analysis 接口，可能包含模型输出的建议，仅供参考。</p>
        """
        return content
    except Exception as e:
        logger.error(f"生成早市简报失败: {e}")
        return generate_default_morning_brief(watchlist)

def generate_midday_review_ai(user_id: str, watchlist: list) -> str:
    logger.info(f"为用户 {str(user_id)[:12]}... 生成中市回顾")
    try:
        indices = get_market_index()
        stock_quotes = []
        for stock in watchlist[:10]:
            code = stock.get("code") or ""
            market = stock.get("market", "A") or "A"
            if not code:
                continue
            q = get_stock_quote(code, market)
            if q:
                stock_quotes.append(q)
        market_context = "<ul>"
        for key, idx in indices.items():
            try:
                change = float(idx.get("change") or 0)
            except Exception:
                change = 0
            market_context += f"<li>{idx.get('name')}: {idx.get('price')} ({('+' if change>0 else '')}{change}%)</li>"
        market_context += "</ul>"
        stocks_context = "<ul>"
        for q in stock_quotes:
            try:
                change = float(q.get("change") or 0)
            except Exception:
                change = 0
            stocks_context += f"<li>{q.get('name')} ({q.get('code')}): {q.get('price')} ({('+' if change>0 else '')}{change}%)</li>"
        stocks_context += "</ul>"
        ai_block = generate_ai_content_for_watchlist(watchlist[:5])
        content = f"""
        <h2>中市回顾</h2>
        <h3>上午市场表现</h3>
        {market_context}
        <h3>自选股表现</h3>
        {stocks_context}
        <h3>AI 简短点评（自选股）</h3>
        {ai_block}
        """
        return content
    except Exception as e:
        logger.error(f"生成中市回顾失败: {e}")
        return generate_default_midday_review(watchlist)

def generate_eod_summary_ai(user_id: str, watchlist: list) -> str:
    logger.info(f"为用户 {str(user_id)[:12]}... 生成尾市总结")
    try:
        indices = get_market_index()
        stock_quotes = []
        for stock in watchlist[:20]:
            code = stock.get("code") or ""
            market = stock.get("market", "A") or "A"
            if not code:
                continue
            q = get_stock_quote(code, market)
            if q:
                stock_quotes.append(q)
        market_context = "<ul>"
        for key, idx in indices.items():
            try:
                change = float(idx.get("change") or 0)
            except Exception:
                change = 0
            market_context += f"<li>{idx.get('name')}: {idx.get('price')} ({('+' if change>0 else '')}{change}%)</li>"
        market_context += "</ul>"
        sorted_by_change = sorted(stock_quotes, key=lambda x: float(x.get("change") or 0), reverse=True)
        top_gainers = sorted_by_change[:3]
        top_losers = sorted_by_change[-3:]
        gain_html = "<ul>"
        for q in top_gainers:
            gain_html += f"<li>{q.get('name')} ({q.get('code')}): {q.get('price')} ({q.get('change')}%)</li>"
        gain_html += "</ul>"
        lose_html = "<ul>"
        for q in top_losers:
            lose_html += f"<li>{q.get('name')} ({q.get('code')}): {q.get('price')} ({q.get('change')}%)</li>"
        lose_html += "</ul>"
        ai_block = generate_ai_content_for_watchlist(watchlist[:5])
        content = f"""
        <h2>尾市总结</h2>
        <h3>今日收盘要点</h3>
        {market_context}
        <h3>涨幅榜（自选股）</h3>
        {gain_html}
        <h3>跌幅榜（自选股）</h3>
        {lose_html}
        <h3>AI 深度点评（自选股）</h3>
        {ai_block}
        """
        return content
    except Exception as e:
        logger.error(f"生成尾市总结失败: {e}")
        return generate_default_eod_summary(watchlist)

# -------------------- 默认回退内容 --------------------
def generate_default_morning_brief(watchlist: list) -> str:
    stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"
    return f"""<h2>📅 早市简报</h2><p>当前 AI/行情服务不可用，采用默认回退。</p><p>您的自选股：{stock_list}</p>"""

def generate_default_midday_review(watchlist: list) -> str:
    stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"
    return f"""<h2>☀️ 中市回顾</h2><p>当前 AI/行情服务不可用，采用默认回退。</p><p>您的自选股：{stock_list}</p>"""

def generate_default_eod_summary(watchlist: list) -> str:
    stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"
    return f"""<h2>🌙 尾市总结</h2><p>当前 AI/行情服务不可用，采用默认回退。</p><p>您的自选股：{stock_list}</p>"""

# -------------------- 邮件创建与发送 --------------------
def create_simple_html(title: str, content: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title></head>
    <body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f4f4f4;">
      <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#f4f4f4;">
        <tr><td style="padding:40px 0;">
          <table width="600" align="center" cellpadding="0" cellspacing="0" role="presentation" style="background:#fff;border-radius:8px;">
            <tr><td style="padding:24px;border-bottom:2px solid #667eea;"><h1 style="margin:0;font-size:20px;color:#333;">{title}</h1></td></tr>
            <tr><td style="padding:24px;">{content}</td></tr>
            <tr><td style="padding:12px;border-top:1px solid #eee;text-align:center;color:#999;font-size:12px;">此邮件由 Portfolio Guardian 自动发送，请勿直接回复</td></tr>
          </table>
        </td></tr>
      </table>
    </body></html>"""

def send_email(to_email: str, subject: str, html_content: str) -> bool:
    try:
        logger.info(f"准备发送邮件到: {to_email} 主题: {subject}")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))
        msg["To"] = to_email
        html_part = MIMEText(html_content, "html", "utf-8")
        msg.attach(html_part)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            logger.info("SMTP TLS 已启用")
            server.login(SMTP_USER, RESEND_API_KEY)
            logger.info("SMTP 登录成功")
            server.send_message(msg)
            logger.info(f"邮件发送成功到 {to_email}")
            return True
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP 认证失败: {e}")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"SMTP 错误: {e}")
        return False
    except Exception as e:
        logger.error(f"发送邮件时出错: {e}")
        return False

# -------------------- 主调度 --------------------
def send_report(report_type: str):
    logger.info("=" * 60)
    report_names = {"morning_brief": "早市简报", "midday_review": "中市回顾", "eod_summary": "尾市总结"}
    title_prefixes = {"morning_brief": "📅 早市简报", "midday_review": "☀️ 中市回顾", "eod_summary": "🌙 尾市总结"}
    logger.info(f"开始执行：{report_names.get(report_type, report_type)}")
    users = get_users_with_email_enabled(report_type)
    if not users:
        logger.warning("没有启用的用户，任务结束")
        return
    logger.info(f"找到 {len(users)} 个启用的用户")
    success_count = 0
    failed_count = 0
    title_prefix = title_prefixes.get(report_type, "📊 股市报告")
    for user in users:
        email = user.get("email") or user.get("contact") or ""
        user_id = user.get("resolved_user_id", "")
        logger.info(f"处理用户: email={email}, user_id={user_id}")
        if not email:
            logger.warning("用户没有设置邮箱，跳过")
            failed_count += 1
            continue
        watchlist = get_user_watchlist(user_id)
        logger.info(f"找到 {len(watchlist)} 只自选股")
        if report_type == "morning_brief":
            content = generate_morning_brief_ai(user_id, watchlist)
        elif report_type == "midday_review":
            content = generate_midday_review_ai(user_id, watchlist)
        elif report_type == "eod_summary":
            content = generate_eod_summary_ai(user_id, watchlist)
        else:
            logger.error(f"未知报告类型: {report_type}")
            failed_count += 1
            continue
        html = create_simple_html(title_prefix, content)
        today = datetime.now().strftime("%Y年%m月%d日 %A")
        subject = f"{title_prefix} - {today}"
        if send_email(email, subject, html):
            success_count += 1
        else:
            failed_count += 1
    logger.info("=" * 60)
    logger.info(f"任务完成: 成功 {success_count}, 失败 {failed_count}")
    logger.info("=" * 60)

# -------------------- CLI --------------------
def main():
    if len(sys.argv) < 2:
        print("用法: python email_system.py <report_type>")
        print("  report_type: morning_brief | midday_review | eod_summary")
        print("示例: STOCK_SCANNER_URL=http://localhost:8000 python email_system.py morning_brief")
        sys.exit(1)
    report_type = sys.argv[1].lower()
    valid_types = ["morning_brief", "midday_review", "eod_summary"]
    if report_type not in valid_types:
        logger.error(f"无效的报告类型: {report_type}")
        logger.error(f"有效类型: {', '.join(valid_types)}")
        sys.exit(1)
    send_report(report_type)

if __name__ == "__main__":
    main()
