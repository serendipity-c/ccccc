#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI驱动邮件发送系统 - 使用 wbsu2003/stock-scanner-mcp 作为信息与 AI 源
并自动根据用户自选股的 code 推断 market_type（A/HK/US），将代码格式化为 stock-scanner-mcp 期望的形式。

注意：所有 URL 与 API keys 已硬编码/或通过环境变量读取。请确保私有管理。
运行前请确保已安装依赖：requests，并已启动 stock-scanner-mcp 服务（默认 http://localhost:8000）。
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
# Resend (SMTP)
RESEND_API_KEY = "re_Nm5shWrw_4Xp8c94P9VFQ12SC7BxEuuv7"
SMTP_HOST = "smtp.resend.com"
SMTP_PORT = 587
SMTP_USER = "resend"
FROM_NAME = "Portfolio Guardian"
FROM_EMAIL = "noreply@chenzhaoqi.asia"

# Supabase (数据库)
SUPABASE_URL = "https://ayjxvejaztusajdntbkh.supabase.co"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF5anh2ZWphenR1c2FqZG50YmtoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQ0ODAxMSwiZXhwIjoyMDg0MDI0MDExfQ.2Ebe2Ft1gPEfyem0Qie9fGaQ8P3uhJvydGBFyCkvIgE"

# 智谱AI (AI 内容生成) - 仍保留作为回退
ZHIPUAI_API_KEY = "21f9ca7cfa0d44f4afeed5ed9d083b23.4zxzk7cZBhr0wnz7"

# stock-scanner-mcp 服务地址（优先使用）
# 可通过环境变量设置，例如： export STOCK_SCANNER_URL="http://localhost:8000"
STOCK_SCANNER_URL = os.environ.get("STOCK_SCANNER_URL", "http://localhost:8000").rstrip("/")

# -------------------- 日志配置 --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -------------------- 惰性导入状态 --------------------
_ZHIPUAI_CLS = None    # None = 未知, False = 缺失, class = ZhipuAI
_logged_missing = set()

# -------------------- 惰性导入帮助函数 --------------------
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
                logger.warning("zhipuai 未安装 — AI 内容生成功能将被禁用或降级。")
                _logged_missing.add("zhipuai")
        except Exception as e:
            _ZHIPUAI_CLS = False
            logger.warning(f"导入 zhipuai 时出错（已降级）：{e}")
    return _ZHIPUAI_CLS if _ZHIPUAI_CLS else None

# -------------------- stock-scanner-mcp HTTP 客户端封装 --------------------
def _call_stock_scanner(path: str, params: dict | None = None, timeout: int = 15) -> dict | str | None:
    """
    调用 stock-scanner-mcp 的 GET 接口并返回解析后的结果（优先 JSON -> 原始文本）。
    path 示例: "/stock_ai_analysis", "/stock_price"
    """
    base = STOCK_SCANNER_URL
    if not base:
        logger.warning("STOCK_SCANNER_URL 未配置，无法调用 stock-scanner-mcp")
        return None
    url = f"{base}{path}"
    try:
        logger.debug(f"调用 stock-scanner-mcp: GET {url} params={params}")
        resp = requests.get(url, params=params or {}, timeout=timeout)
        if resp.status_code != 200:
            logger.warning(f"stock-scanner-mcp {url} 返回 {resp.status_code}: {resp.text[:200]}")
            return None
        # 尝试解析 JSON
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

# -------------------- zhipuai 封装（回退） --------------------
def get_zhipu_client():
    """返回 zhipuai 客户端实例，若不可用返回 None"""
    if not ZHIPUAI_API_KEY:
        logger.warning("未设置 ZHIPUAI_API_KEY；AI 内容生成将被禁用。")
        return None

    ZhipuAI_cls = _import_zhipuai_class()
    if not ZhipuAI_cls:
        return None

    try:
        return ZhipuAI_cls(api_key=ZHIPUAI_API_KEY)
    except Exception as e:
        logger.error(f"初始化智谱AI客户端失败: {e}")
        return None

def _call_zhipu(prompt: str) -> str | None:
    """向 zhipuai 发送 prompt 并返回文本（尽量）"""
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

# -------------------- 市场类型推断与代码格式化 --------------------
def infer_market_and_format(raw_code: str) -> tuple[str, str]:
    """
    根据原始字符串推断 market_type ('A','HK','US') 并格式化 code 为 stock-scanner-mcp 推荐的形式:
      - A 股: 返回纯数字代码（例如 '600795'），market_type='A'
      - 港股: 返回不带前缀的代码（例如 '01810' 或 '810' 依赖上游），market_type='HK'
      - 美股: 返回标准 ticker（例如 'AAPL'），market_type='US'
    规则（启发式）:
      - 带前缀 sh/sz => A 股
      - 带前缀 hk 或 包含 .HK/后缀 => HK
      - 带前缀 us / gb_ / 全字母短代码 => US
      - 纯数字且长度==6 => A
      - 纯数字且长度 in (4,5) => HK
      - 否则默认尝试作为 US（ticker）
    """
    if not raw_code:
        return ("", "A")
    s = str(raw_code).strip()
    s_low = s.lower()

    # remove common separators
    s_clean = s.replace(".", "").replace("-", "").replace("_", "").strip()

    # explicit prefixes
    m = re.match(r'^(sh|sz)(0*\d+)$', s_low)
    if m:
        return (m.group(2).lstrip("0") or m.group(2), "A")
    m = re.match(r'^(hk)(0*\d+)$', s_low)
    if m:
        return (m.group(2).lstrip("0") or m.group(2), "HK")

    # patterns like '600795' -> A (6 digits)
    if re.fullmatch(r'\d{6}', s_clean):
        return (s_clean.lstrip("0") or s_clean, "A")
    # 4-5 digits -> likely HK
    if re.fullmatch(r'\d{4,5}', s_clean):
        return (s_clean.lstrip("0") or s_clean, "HK")

    # patterns with .hk suffix (e.g., 0005.hk)
    m = re.match(r'^(\d{1,6})hk$', s_low)
    if m:
        return (m.group(1).lstrip("0") or m.group(1), "HK")

    # gb_ or us prefixes
    if s_low.startswith("us"):
        return (s[2:].upper(), "US")
    if s_low.startswith("gb_"):
        return (s[3:].upper(), "US")

    # if contains letters and no digits (likely ticker)
    if re.fullmatch(r'[a-zA-Z]{1,6}', s_clean):
        return (s_clean.upper(), "US")

    # fallback: if contains letters mixed with digits (e.g. 'AAPL.US'), extract letters part -> US
    m = re.match(r'^([A-Za-z]+)', s_clean)
    if m:
        return (m.group(1).upper(), "US")

    # default to A with numeric portion
    digits = re.sub(r'\D', '', s_clean)
    if digits:
        if len(digits) == 6:
            return (digits, "A")
        if len(digits) in (4,5):
            return (digits, "HK")
        return (digits, "A")

    # final fallback
    return (s_clean.upper(), "US")

# -------------------- Supabase 与自选股读取（包含格式化） --------------------
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
    logger.warning(f"未能通过常见表解析 email={email} 对应的 user_id")
    return None

def get_users_with_email_enabled(report_type: str = "morning_brief") -> list[dict]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("SUPABASE_URL 或 SUPABASE_SERVICE_KEY 未设置；无法查询用户列表。")
        return []

    headers = _supabase_headers()
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_email_preferences"
    params = {
        "select": "*",
        "enabled": "eq.true",
        f"{report_type}->>enabled": "eq.true"
    }

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
    从 Supabase 获取用户自选股，并为每条记录推断 market_type 与格式化 code。
    返回项格式：{"name": ..., "raw_code": ..., "code": formatted_code, "market": "A"/"HK"/"US"}
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

        formatted_code, market = infer_market_and_format(raw_code)
        # If no code in DB but name looks like a ticker, try to infer from name
        if not formatted_code and name:
            f2, m2 = infer_market_and_format(name)
            if f2:
                formatted_code, market = f2, m2

        normalized.append({
            "name": name or formatted_code,
            "raw_code": raw_code,
            "code": formatted_code,
            "market": market
        })
    logger.info(f"   用户 {user_id} 有 {len(normalized)} 条自选股（含格式化 code 与 market）")
    return normalized

# -------------------- 使用 stock-scanner-mcp 的行情/AI 封装 --------------------
def get_stock_quote(stock_code: str, market_type: str = "A") -> dict | None:
    """
    使用 stock-scanner-mcp /stock_price 获取单只股票行情。
    stock_code: formatted code (e.g., '600795' for A, '01810' for HK, 'AAPL' for US)
    market_type: 'A' / 'HK' / 'US'
    返回统一结构：{code,name,price,change,volume,high,low,open,yesterday_close}
    """
    if not stock_code:
        return None
    params = {"stock_code": stock_code, "market_type": market_type}
    res = _call_stock_scanner("/stock_price", params)
    if not res:
        return None
    if isinstance(res, dict):
        if "data" in res and isinstance(res["data"], dict):
            src = res["data"]
        else:
            src = res
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
    """
    使用 stock-scanner-mcp 获取主要指数（上证、深证、创业板）。
    """
    indices_codes = {
        "sh": ("000001", "A"),
        "sz": ("399001", "A"),
        "cyb": ("399006", "A"),
    }
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
    """
    使用 stock-scanner-mcp 的 /stock_ai_analysis 获取单支股票的 AI 分析（返回文本或 HTML）。
    """
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

# -------------------- AI 内容生成统一入口（优先使用 stock-scanner-mcp, 再 zhipuai） --------------------
def generate_ai_content_for_watchlist(watchlist: list) -> str:
    """
    为一组自选股生成聚合 AI 内容：使用 stock-scanner-mcp 的 /stock_ai_analysis（按 stock 的 market 调用）。
    """
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
            # 回退到 zhipuai（若可用）
            prompt = f"请对股票 {name} ({code}, 市场 {market}) 做简短分析，包含趋势与操作建议（中文，约100字）。"
            z = _call_zhipu(prompt)
            if z:
                parts.append(f"<h3>{name} ({code} - {market})</h3><div>{z}</div>")
            else:
                parts.append(f"<p><strong>{name} ({code})</strong>：无法获取 AI 分析，使用回退简述。</p>")
    if not parts:
        return "<p>暂无可用自选股分析。</p>"
    return "\n".join(parts)

# -------------------- 报告生成（基于 stock-scanner-mcp） --------------------
def generate_morning_brief_ai(user_id: str, watchlist: list) -> str:
    logger.info(f"为用户 {str(user_id)[:12]}... 生成早市简报（使用 stock-scanner-mcp）")
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
    logger.info(f"为用户 {str(user_id)[:12]}... 生成中市回顾（使用 stock-scanner-mcp）")
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
    logger.info(f"为用户 {str(user_id)[:12]}... 生成尾市总结（使用 stock-scanner-mcp）")
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
    return f"""
    <h2>📅 早市简报</h2>
    <p>当前 AI 服务或行情服务不可用。使用默认回退内容。</p>
    <p>您的自选股：{stock_list}</p>
    <p>提示：请关注今日开盘及自选股表现。</p>
    """

def generate_default_midday_review(watchlist: list) -> str:
    stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"
    return f"""
    <h2>☀️ 中市回顾</h2>
    <p>当前 AI 服务或行情服务不可用。使用默认回退内容。</p>
    <p>您的自选股：{stock_list}</p>
    <p>提示：请关注午后走势。</p>
    """

def generate_default_eod_summary(watchlist: list) -> str:
    stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"
    return f"""
    <h2>🌙 尾市总结</h2>
    <p>当前 AI 服务或行情服务不可用。使用默认回退内容。</p>
    <p>您的自选股：{stock_list}</p>
    <p>提示：请查看自选股今日表现并做好盘后总结。</p>
    """

# -------------------- 邮件创建与发送（使用 SMTP） --------------------
def create_simple_html(title: str, content: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title></head>
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
</body>
</html>"""

def send_email(to_email: str, subject: str, html_content: str) -> bool:
    """通过 SMTP 发送邮件（使用硬编码的 RESEND_API_KEY 作为密码）"""
    try:
        logger.info(f"准备发送邮件到: {to_email}")
        logger.info(f"   主题: {subject}")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))
        msg["To"] = to_email

        html_part = MIMEText(html_content, "html", "utf-8")
        msg.attach(html_part)

        logger.info(f"   连接到 SMTP 服务器: {SMTP_HOST}:{SMTP_PORT}")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            logger.info("   TLS 已启用")
            server.login(SMTP_USER, RESEND_API_KEY)
            logger.info("   SMTP 登录成功")
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

# -------------------- 主调度与报告发送 --------------------
def send_report(report_type: str):
    logger.info("=" * 60)
    report_names = {"morning_brief": "早市简报", "midday_review": "中市回顾", "eod_summary": "尾市总结"}
    title_prefixes = {"morning_brief": "📅 早市简报", "midday_review": "☀️ 中市回顾", "eod_summary": "🌙 尾市总结"}

    logger.info(f"开始执行：{report_names.get(report_type, report_type)}")
    logger.info("=" * 60)

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

        logger.info(f"\n处理用户: email={email}, user_id={user_id}")
        logger.info(f"   邮箱: {email}")

        if not email:
            logger.warning("   用户没有设置邮箱，跳过")
            failed_count += 1
            continue

        logger.info("   获取用户自选股...")
        watchlist = get_user_watchlist(user_id)
        logger.info(f"   找到 {len(watchlist)} 只自选股")

        logger.info("   使用 AI 生成个性化内容...")
        if report_type == "morning_brief":
            content = generate_morning_brief_ai(user_id, watchlist)
        elif report_type == "midday_review":
            content = generate_midday_review_ai(user_id, watchlist)
        elif report_type == "eod_summary":
            content = generate_eod_summary_ai(user_id, watchlist)
        else:
            logger.error(f"未知的报告类型: {report_type}")
            failed_count += 1
            continue

        html = create_simple_html(title_prefix, content)
        today = datetime.now().strftime("%Y年%m月%d日 %A")
        subject = f"{title_prefix} - {today}"

        if send_email(email, subject, html):
            success_count += 1
        else:
            failed_count += 1

    logger.info("\n" + "=" * 60)
    logger.info(f"任务完成: 成功 {success_count}, 失败 {failed_count}")
    logger.info("=" * 60)

# -------------------- CLI 主函数 --------------------
def main():
    if len(sys.argv) < 2:
        print("用法: python email_system.py <report_type>")
        print("")
        print("报告类型:")
        print("  morning_brief  - 早市简报 (08:30)")
        print("  midday_review  - 中市回顾 (12:00)")
        print("  eod_summary    - 尾市总结 (16:30)")
        print("")
        print("示例:")
        print("  STOCK_SCANNER_URL=http://localhost:8000 python email_system.py morning_brief")
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
