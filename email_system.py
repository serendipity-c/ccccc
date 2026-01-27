#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 驱动邮件发送系统 - 使用 DR-lin-eng/stock-scanner 作为唯一信息与 AI 源
说明：
- 本版将所有行情与 AI 分析调用改为请求 DR-lin-eng/stock-scanner 的 Web API（见仓库 web_app.py / README）。
- 主要使用的 HTTP 接口（需在部署的 stock-scanner 服务中存在）：
    POST {DR_STOCK_SCANNER_URL}/api/analyze         -> 单支股票分析（返回 price_info, ai_analysis, 等）
    POST {DR_STOCK_SCANNER_URL}/api/analyze_stream  -> （可选）流式分析
    POST {DR_STOCK_SCANNER_URL}/api/batch-analyze   -> 批量分析（可选）
  如果你的部署使用不同路径，请告知我以便调整。
- 配置：通过环境变量 DR_STOCK_SCANNER_URL 设置服务地址，例如:
    export DR_STOCK_SCANNER_URL="http://localhost:8443"
- 回退：若 DR 服务不可用，脚本会尝试使用内置 zhipuai（若配置）作为 AI 回退；行情/价格若不可用则使用默认回退文案。
"""

from __future__ import annotations

import os
import re
import sys
import time
import smtplib
import json
import logging
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from requests.exceptions import ConnectionError as RequestsConnectionError

# -------------------- 配置（可移到环境变量或 Secrets） --------------------
RESEND_API_KEY = "re_Nm5shWrw_4Xp8c94P9VFQ12SC7BxEuuv7"
SMTP_HOST = "smtp.resend.com"
SMTP_PORT = 587
SMTP_USER = "resend"
FROM_NAME = "Portfolio Guardian"
FROM_EMAIL = "noreply@chenzhaoqi.asia"

SUPABASE_URL = "https://ayjxvejaztusajdntbkh.supabase.co"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF5anh2ZWphenR1c2FqZG50YmtoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQ0ODAxMSwiZXhwIjoyMDg0MDI0MDExfQ.2Ebe2Ft1gPEfyem0Qie9fGaQ8P3uhJvydGBFyCkvIgE"

ZHIPUAI_API_KEY = "21f9ca7cfa0d44f4afeed5ed9d083b23.4zxzk7cZBhr0wnz7"

# DR-lin-eng/stock-scanner 服务地址（优先使用）
DR_STOCK_SCANNER_URL = os.environ.get("DR_STOCK_SCANNER_URL", "").rstrip("/")

# -------------------- 日志 --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------- 惰性导入状态 --------------------
_ZHIPUAI_CLS = None
_logged_missing = set()

# -------------------- zhipuai client (回退) --------------------
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
                logger.warning("zhipuai 未安装 — AI 回退不可用。")
                _logged_missing.add("zhipuai")
        except Exception as e:
            _ZHIPUAI_CLS = False
            logger.warning(f"导入 zhipuai 时出错：{e}")
    return _ZHIPUAI_CLS if _ZHIPUAI_CLS else None

def get_zhipu_client():
    if not ZHIPUAI_API_KEY:
        return None
    cls = _import_zhipuai_class()
    if not cls:
        return None
    try:
        return cls(api_key=ZHIPUAI_API_KEY)
    except Exception as e:
        logger.error(f"初始化 zhipuai 失败: {e}")
        return None

def call_zhipu(prompt: str) -> str | None:
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

# -------------------- DR stock-scanner HTTP 客户端 --------------------
def _call_dr_scanner(path: str, payload: dict | None = None, timeout: int = 20) -> dict | str | None:
    """
    对 DR-lin-eng/stock-scanner 的 HTTP 接口进行调用。
    常用 endpoints:
      POST {DR_STOCK_SCANNER_URL}/api/analyze  -> body: {"stock_code": "..."}
      POST {DR_STOCK_SCANNER_URL}/api/analyze_stream -> body: {"stock_code": "..."} (stream)
      POST {DR_STOCK_SCANNER_URL}/api/batch-analyze -> body: {"stock_list": [...]}
    返回 JSON（优先）或文本。
    """
    if not DR_STOCK_SCANNER_URL:
        logger.warning("DR_STOCK_SCANNER_URL 未配置，无法调用 DR stock-scanner 服务")
        return None

    url = f"{DR_STOCK_SCANNER_URL}{path}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        logger.debug(f"POST {url} payload={payload}")
        resp = requests.post(url, json=payload or {}, headers=headers, timeout=timeout)
        if resp.status_code not in (200, 201):
            logger.warning(f"DR scanner {url} 返回 {resp.status_code}: {resp.text[:300]}")
            return None
        try:
            return resp.json()
        except Exception:
            return resp.text
    except RequestsConnectionError as e:
        logger.warning(f"无法连接 DR scanner ({url}): {e}")
        return None
    except Exception as e:
        logger.debug(f"���用 DR scanner 出错: {e}")
        return None

# -------------------- 名称到代码解析（若 user_watchlist 仅有 name） --------------------
def infer_market_and_format(raw_code: str) -> tuple[str, str]:
    """启发式将原始可疑代码/字符串映射为 (formatted_code, market)"""
    if not raw_code:
        return ("", "A")
    s = str(raw_code).strip()
    s_low = s.lower()
    s_clean = re.sub(r'[\s\-_\.]', '', s_low)
    m = re.match(r'^(sh|sz)(0*\d+)$', s_clean)
    if m:
        return (m.group(2).lstrip("0") or m.group(2), "A")
    m = re.match(r'^(hk)(0*\d+)$', s_clean)
    if m:
        return (m.group(2).lstrip("0") or m.group(2), "HK")
    if re.fullmatch(r'\d{6}', s_clean):
        return (s_clean.lstrip("0") or s_clean, "A")
    if re.fullmatch(r'\d{4,5}', s_clean):
        return (s_clean.lstrip("0") or s_clean, "HK")
    m = re.match(r'^(\d{1,6})hk$', s_clean)
    if m:
        return (m.group(1).lstrip("0") or m.group(1), "HK")
    if s_clean.startswith("us"):
        return (s_clean[2:].upper(), "US")
    if s_clean.startswith("gb"):
        return (s_clean[2:].upper(), "US")
    if re.fullmatch(r'[a-zA-Z]{1,6}', s_clean):
        return (s_clean.upper(), "US")
    m = re.match(r'^([A-Za-z]+)', s_clean)
    if m:
        return (m.group(1).upper(), "US")
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

def resolve_code_by_name(name: str) -> tuple[str, str] | tuple[None, None]:
    """
    尝试将名称解析为 (code, market)：
      1) 直接从 name 提取
      2) 调用 DR scanner 的可能搜索 endpoint（若部署）
      3) 启发式回退
    """
    if not name:
        return (None, None)
    direct = _extract_code_from_name(name)
    if direct:
        code, market = infer_market_and_format(direct)
        if code:
            return (code, market)
    # 尝试调用 DR 的搜索端点 (仓库未强制规定精确名称，try common paths)
    if DR_STOCK_SCANNER_URL:
        search_endpoints = ["/api/search", "/api/suggest", "/api/lookup", "/api/stock_search"]
        payloads = [{"q": name}, {"query": name}, {"keyword": name}, {"stock_name": name}]
        for ep in search_endpoints:
            for p in payloads:
                try:
                    res = _call_dr_scanner(ep, p, timeout=6)
                    if not res:
                        continue
                    # parse result
                    if isinstance(res, dict):
                        # Try data/results array
                        candidates = []
                        for key in ("data", "results", "items"):
                            if key in res and isinstance(res[key], list):
                                candidates = res[key]
                                break
                        if not candidates:
                            candidates = [res]
                    elif isinstance(res, list):
                        candidates = res
                    else:
                        candidates = []
                    for item in candidates:
                        if not isinstance(item, dict):
                            continue
                        for key in ("code", "stock_code", "symbol", "ticker", "id"):
                            if key in item and item[key]:
                                c, m = infer_market_and_format(str(item[key]))
                                if c:
                                    return (c, m)
                        # values scanning
                        for v in item.values():
                            if isinstance(v, str) and re.fullmatch(r'\d{4,6}', v):
                                c, m = infer_market_and_format(v)
                                if c:
                                    return (c, m)
                except Exception:
                    continue
    # fallback heuristics
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

# -------------------- Supabase helpers --------------------
def _supabase_headers():
    return {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "Content-Type": "application/json"}

def get_user_id_by_email(email: str) -> str | None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.warning("Supabase 未配置")
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
                return str(uid)
            for v in first.values():
                if v:
                    return str(v)
        elif resp.status_code == 404:
            continue
    return None

def get_users_with_email_enabled(report_type: str = "morning_brief") -> list[dict]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("Supabase 未配置")
        return []
    headers = _supabase_headers()
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_email_preferences"
    params = {"select": "*", "enabled": "eq.true", f"{report_type}->>enabled": "eq.true"}
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
    从 Supabase 读取 user_watchlist 表（select="*")，兼容只有 name 的情况，
    并尝试解析 code 与 market（A/HK/US）。
    返回每条：{"name","raw_code","code","market"}
    """
    if not user_id:
        return []
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("Supabase 未配置")
        return []
    headers = _supabase_headers()
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_watchlist"
    params = {"select": "*", "user_id": f"eq.{user_id}"}
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
        raw_code = row.get("code") or row.get("symbol") or row.get("stock_code") or row.get("ticker") or row.get("id") or ""
        name = str(name).strip() if name is not None else ""
        raw_code = str(raw_code).strip() if raw_code is not None else ""
        if raw_code:
            formatted_code, market = infer_market_and_format(raw_code)
            if not formatted_code:
                alt_code, alt_market = resolve_code_by_name(name)
                if alt_code:
                    formatted_code, market = alt_code, alt_market
        else:
            formatted_code, market = resolve_code_by_name(name)
        normalized.append({"name": name or formatted_code or raw_code, "raw_code": raw_code, "code": formatted_code or "", "market": market or "A"})
    return normalized

# -------------------- DR scanner 基于 /api/analyze 的行情与 AI 解析封装 --------------------
def get_ai_analysis_for_stock(stock_code: str, market_type: str = "A") -> str | None:
    """
    使用 DR-lin-eng/stock-scanner 的 /api/analyze（POST）获取单支股票分析（包含 AI 段落）。
    期望返回包括 ai_analysis / price_info 等字段（根据仓库实现）。
    """
    if not stock_code:
        return None
    payload = {"stock_code": stock_code}
    # some deployments might expect market type too
    if market_type:
        payload["market_type"] = market_type
    res = _call_dr_scanner("/api/analyze", payload, timeout=30)
    if not res:
        return None
    # parse typical fields from repository's analyzer outputs
    if isinstance(res, dict):
        # try common keys
        for key in ("ai_analysis", "ai", "analysis", "report", "result"):
            if key in res and res[key]:
                if isinstance(res[key], dict) and "content" in res[key]:
                    return res[key]["content"]
                return res[key]
        # fallback: if report contains 'ai_analysis' inside nested 'data'
        data = res.get("data") if isinstance(res.get("data"), dict) else None
        if data:
            for key in ("ai_analysis", "ai", "analysis"):
                if key in data and data[key]:
                    return data[key]
        # else stringify main message
        return json.dumps(res, ensure_ascii=False)
    return str(res)

def get_stock_quote(stock_code: str, market_type: str = "A") -> dict | None:
    """
    尝试通过 /api/analyze 来获取股票的 price_info（仓库将价格、指标包含在分析结果中）。
    解析返回中常见 price_info 字段（current_price / price_info / price）
    """
    if not stock_code:
        return None
    payload = {"stock_code": stock_code}
    if market_type:
        payload["market_type"] = market_type
    res = _call_dr_scanner("/api/analyze", payload, timeout=20)
    if not res:
        return None
    if isinstance(res, dict):
        src = res.get("price_info") or res.get("data", {}).get("price_info") if isinstance(res.get("data"), dict) else None
        if not src:
            # try common top-level price fields
            src = {}
            if "current_price" in res:
                src["price"] = res.get("current_price")
            if "price" in res:
                src["price"] = res.get("price")
        # normalize
        try:
            price = src.get("price") or src.get("current_price") or 0
        except Exception:
            price = 0
        mapped = {
            "code": stock_code,
            "name": src.get("name") or res.get("stock_name") or "",
            "price": price,
            "change": src.get("price_change") or src.get("change") or res.get("change") or 0,
            "volume": src.get("volume") or 0,
            "amount": src.get("amount") or 0,
            "high": src.get("high") or 0,
            "low": src.get("low") or 0,
            "open": src.get("open") or 0,
            "yesterday_close": src.get("yesterday_close") or src.get("pre_close") or 0,
        }
        return mapped
    return None

def get_market_index() -> dict:
    """基于 get_stock_quote 调用获取主要指数（上证/深证/创业板）"""
    indices = {"sh": ("000001", "A"), "sz": ("399001", "A"), "cyb": ("399006", "A")}
    out = {}
    for k, (code, mt) in indices.items():
        # try variants
        candidates = [code, f"sh{code}", f"sz{code}"]
        q = None
        for c in candidates:
            q = get_stock_quote(c, mt)
            if q:
                break
        if q:
            out[k] = {"name": q.get("name") or k, "code": code, "price": q.get("price"), "change": q.get("change")}
    return out

# -------------------- 组合生成 AI 报告段落 --------------------
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
            prompt = f"请给出对股票 {name} ({code}) 的简短分析（中文，约100字）。"
            z = call_zhipu(prompt)
            if z:
                parts.append(f"<h3>{name} ({code})</h3><div>{z}</div>")
            else:
                parts.append(f"<p><strong>{name} ({code})</strong>：无法获取 AI 分析。</p>")
    return "\n".join(parts) if parts else "<p>暂无可用自选股分析。</p>"

# -------------------- 报告生成函数 --------------------
def generate_morning_brief_ai(user_id: str, watchlist: list) -> str:
    try:
        indices = get_market_index()
        stock_context = generate_ai_content_for_watchlist(watchlist)
        indices_html = ""
        if indices:
            indices_html = "<ul>" + "".join([f"<li>{idx['name']}: {idx['price']} ({idx['change']})</li>" for idx in indices.values()]) + "</ul>"
        content = f"""
            <h2>早市简报</h2>
            <h3>市场要点</h3>
            {indices_html}
            <h3>自选股深度分析</h3>
            {stock_context}
        """
        return content
    except Exception as e:
        logger.error(f"生成早市简报失败: {e}")
        return generate_default_morning_brief(watchlist)

def generate_midday_review_ai(user_id: str, watchlist: list) -> str:
    try:
        indices = get_market_index()
        stock_quotes = []
        for s in watchlist[:10]:
            code = s.get("code") or s.get("raw_code") or ""
            market = s.get("market", "A") or "A"
            if not code:
                continue
            q = get_stock_quote(code, market)
            if q:
                stock_quotes.append(q)
        market_html = "<ul>" + "".join([f"<li>{v['name']}: {v['price']} ({v['change']})</li>" for v in indices.values()]) + "</ul>" if indices else ""
        stocks_html = "<ul>" + "".join([f"<li>{q['name']} ({q['code']}): {q['price']} ({q['change']})</li>" for q in stock_quotes]) + "</ul>"
        ai_block = generate_ai_content_for_watchlist(watchlist[:5])
        return f"<h2>中市回顾</h2><h3>上午市场表现</h3>{market_html}<h3>自选股</h3>{stocks_html}<h3>AI点评</h3>{ai_block}"
    except Exception as e:
        logger.error(f"生成中市回顾失败: {e}")
        return generate_default_midday_review(watchlist)

def generate_eod_summary_ai(user_id: str, watchlist: list) -> str:
    try:
        indices = get_market_index()
        stock_quotes = []
        for s in watchlist[:20]:
            code = s.get("code") or s.get("raw_code") or ""
            market = s.get("market", "A") or "A"
            if not code:
                continue
            q = get_stock_quote(code, market)
            if q:
                stock_quotes.append(q)
        sorted_by_change = sorted(stock_quotes, key=lambda x: float(x.get("change") or 0), reverse=True)
        gain_html = "<ul>" + "".join([f"<li>{q['name']} ({q['code']}): {q['price']} ({q['change']})</li>" for q in sorted_by_change[:3]]) + "</ul>"
        lose_html = "<ul>" + "".join([f"<li>{q['name']} ({q['code']}): {q['price']} ({q['change']})</li>" for q in sorted_by_change[-3:]]) + "</ul>"
        ai_block = generate_ai_content_for_watchlist(watchlist[:5])
        market_html = "<ul>" + "".join([f"<li>{v['name']}: {v['price']} ({v['change']})</li>" for v in indices.values()]) + "</ul>" if indices else ""
        return f"<h2>尾市总结</h2><h3>收盘要点</h3>{market_html}<h3>涨幅榜</h3>{gain_html}<h3>跌幅榜</h3>{lose_html}<h3>AI点评</h3>{ai_block}"
    except Exception as e:
        logger.error(f"生成尾市总结失败: {e}")
        return generate_default_eod_summary(watchlist)

# -------------------- 默认回退 --------------------
def generate_default_morning_brief(watchlist: list) -> str:
    stock_list = ", ".join([s.get("name", "") for s in (watchlist or [])[:5]]) or "暂无自选股"
    return f"<h2>早市简报（回退）</h2><p>您的自选股：{stock_list}</p>"

def generate_default_midday_review(watchlist: list) -> str:
    stock_list = ", ".join([s.get("name", "") for s in (watchlist or [])[:5]]) or "暂无自选股"
    return f"<h2>中市回顾（回退）</h2><p>您的自选股：{stock_list}</p>"

def generate_default_eod_summary(watchlist: list) -> str:
    stock_list = ", ".join([s.get("name", "") for s in (watchlist or [])[:5]]) or "暂无自选股"
    return f"<h2>尾市总结（回退）</h2><p>您的自选股：{stock_list}</p>"

# -------------------- 邮件发送 --------------------
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
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))
        msg["To"] = to_email
        html_part = MIMEText(html_content, "html", "utf-8")
        msg.attach(html_part)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, RESEND_API_KEY)
            server.send_message(msg)
        logger.info(f"邮件发送成功到 {to_email}")
        return True
    except Exception as e:
        logger.error(f"发送邮件失败: {e}")
        return False

# -------------------- 主发送流程 --------------------
def send_report(report_type: str):
    logger.info(f"开始发送报告: {report_type}")
    users = get_users_with_email_enabled(report_type)
    if not users:
        logger.warning("没有启用的用户")
        return
    success = 0
    failed = 0
    for user in users:
        email = user.get("email") or user.get("contact") or ""
        uid = user.get("resolved_user_id", "")
        if not email:
            failed += 1
            continue
        watchlist = get_user_watchlist(uid)
        if report_type == "morning_brief":
            content = generate_morning_brief_ai(uid, watchlist)
            title_prefix = "📅 早市简报"
        elif report_type == "midday_review":
            content = generate_midday_review_ai(uid, watchlist)
            title_prefix = "☀️ 中市回顾"
        elif report_type == "eod_summary":
            content = generate_eod_summary_ai(uid, watchlist)
            title_prefix = "🌙 尾市总结"
        else:
            logger.error("未知报告类型")
            failed += 1
            continue
        html = create_simple_html(title_prefix, content)
        subject = f"{title_prefix} - {datetime.now().strftime('%Y年%m月%d日 %A')}"
        if send_email(email, subject, html):
            success += 1
        else:
            failed += 1
    logger.info(f"完成: 成功 {success}, 失败 {failed}")

# -------------------- CLI 入口 --------------------
def main():
    if len(sys.argv) < 2:
        print("用法: python email_system.py <report_type>")
        print("report_type: morning_brief | midday_review | eod_summary")
        print("示例: DR_STOCK_SCANNER_URL=http://localhost:8443 python email_system.py morning_brief")
        sys.exit(1)
    report_type = sys.argv[1].lower()
    valid = ["morning_brief", "midday_review", "eod_summary"]
    if report_type not in valid:
        logger.error("无效报告类型")
        sys.exit(1)
    send_report(report_type)

if __name__ == "__main__":
    main()
