#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-driven email report system (full replacement)

Features / improvements:
- Read sensitive keys from environment variables (recommended) with optional fallbacks.
- Supabase access: fetch enabled users from user_email_preferences, resolve user_id by email,
  then fetch user's watchlist from user_watchlist (name column).
- Lazy imports for akshare and zhipuai with single-time warnings and graceful degradation.
- akshare compatibility layer with candidate function names and retry/backoff for transient network errors.
- zhipuai compatibility layer tolerant to different SDK return shapes.
- Robust logging and non-fatal failures: system will continue sending emails even if AI or market data is unavailable.
- Keep API for calling: `python email_system.py <report_type>` where report_type in
  ['morning_brief', 'midday_review', 'eod_summary'].

IMPORTANT:
- Store sensitive values (SUPABASE_SERVICE_KEY, RESEND_API_KEY, ZHIPUAI_API_KEY, etc.)
  in environment variables or GitHub Secrets and do NOT hardcode them in source.
"""

from __future__ import annotations

import os
import sys
import time
import smtplib
import requests
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from http.client import RemoteDisconnected
from requests.exceptions import ConnectionError as RequestsConnectionError

# -------------------- Configuration / Secrets --------------------
# Prefer environment variables. If you must use hardcoded values for local testing,
# set them here (NOT recommended for production).
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")            # Resend SMTP/API key
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.resend.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "resend")
FROM_NAME = os.getenv("FROM_NAME", "Portfolio Guardian")
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@example.com")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")               # e.g. https://<project-ref>.supabase.co
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

ZHIPUAI_API_KEY = os.getenv("ZHIPUAI_API_KEY", "")

# -------------------- Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -------------------- Lazy import state --------------------
_AKSHARE = None        # None = unknown, False = missing, module = imported
_ZHIPUAI_CLS = None    # None = unknown, False = missing, class = ZhipuAI
_logged_missing = set()


# -------------------- Lazy import helpers --------------------
def _import_akshare():
    """Lazily import akshare. Return module or None if not available."""
    global _AKSHARE, _logged_missing
    if _AKSHARE is None:
        try:
            import akshare as ak
            _AKSHARE = ak
            logger.info("akshare imported")
        except ImportError:
            _AKSHARE = False
            if "akshare" not in _logged_missing:
                logger.warning("akshare 未安装 — 与行情/新闻相关功能将被禁用或降级。")
                _logged_missing.add("akshare")
        except Exception as e:
            _AKSHARE = False
            logger.warning(f"导入 akshare 时出错（已降级）：{e}")
    return _AKSHARE if _AKSHARE else None


def _import_zhipuai_class():
    """Lazily import ZhipuAI class from zhipuai SDK. Return class or None if unavailable."""
    global _ZHIPUAI_CLS, _logged_missing
    if _ZHIPUAI_CLS is None:
        try:
            from zhipuai import ZhipuAI  # type: ignore
            _ZHIPUAI_CLS = ZhipuAI
            logger.info("zhipuai SDK imported")
        except ImportError:
            _ZHIPUAI_CLS = False
            if "zhipuai" not in _logged_missing:
                logger.warning("zhipuai 未安装 — AI 内容生成功能将被禁用或降级。")
                _logged_missing.add("zhipuai")
        except Exception as e:
            _ZHIPUAI_CLS = False
            logger.warning(f"导入 zhipuai 时出错（已降级）：{e}")
    return _ZHIPUAI_CLS if _ZHIPUAI_CLS else None


# -------------------- zhipuai wrapper --------------------
def get_zhipu_client():
    """Return a zhipuai client instance or None if unavailable."""
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


def generate_ai_content(prompt: str) -> str | None:
    """
    Generate content using zhipuai. Return string or None on failure (caller should fallback).
    This function is tolerant to different SDK response shapes.
    """
    try:
        client = get_zhipu_client()
        if not client:
            return None

        logger.info("正在调用智谱AI生成内容...")
        # Many versions provide client.chat.completions.create; some return dicts.
        try:
            response = client.chat.completions.create(
                model="glm-4-flash",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=2000
            )
        except Exception as e:
            # Some SDKs might expose different methods; try a fallback call if available
            try:
                # e.g. client.create in some custom wrappers
                response = client.create(prompt=prompt)
            except Exception:
                raise e

        # Parse response robustly
        content = None
        try:
            if hasattr(response, "choices"):
                # Typical object-based SDK
                content = getattr(response.choices[0].message, "content", None) \
                          or getattr(response.choices[0], "text", None)
            elif isinstance(response, dict):
                choices = response.get("choices") or []
                if choices:
                    first = choices[0]
                    content = (first.get("message") or {}).get("content") or first.get("text") or None
            else:
                # Last resort: try to stringize
                content = str(response)
        except Exception:
            content = None

        if not content:
            logger.warning("AI 返回空内容或解析失败，将使用默认回退。")
            return None

        logger.info("AI 内容生成成功")
        return content

    except Exception as e:
        logger.error(f"AI 生成内容失败: {e}")
        return None


# -------------------- Supabase helpers --------------------
def _supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json"
    }


def get_user_id_by_email(email: str) -> str | None:
    """
    Resolve a user_id by email. Tries common user tables: users, user_profiles, profiles.
    Returns the first found id (string) or None.
    """
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
            # Try common id fields
            uid = first.get("user_id") or first.get("id")
            if uid:
                logger.info(f"通过表 {table} 找到 user_id={uid} for email={email}")
                return str(uid)
            # fallback: first non-empty value
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
    """
    Query user_email_preferences for users who enabled `report_type`.
    Adds a 'resolved_user_id' key to each record (empty string if unresolved).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("SUPABASE_URL 或 SUPABASE_SERVICE_KEY 未设置；无法查询用户列表。")
        return []

    headers = _supabase_headers()
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_email_preferences"
    # If your table stores each report preference in a JSONB column, the JSON path filter is used.
    params = {
        "select": "*",
        "enabled": "eq.true",
        f"{report_type}->>enabled": "eq.true"
    }

    logger.info(f"查询启用了 {report_type} 的用户...")
    logger.debug(f"请求 Supabase: GET {url} params={params}")
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

    logger.info(f"   找到 {len(records)} 个启用的用户条目")

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
    Fetch user's watchlist from user_watchlist table.
    Only select the 'name' column (as requested). Return list of dicts with keys 'name' and 'code' (code empty).
    """
    if not user_id:
        return []

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("SUPABASE_URL 或 SUPABASE_SERVICE_KEY 未设置；无法查询自选股。")
        return []

    headers = _supabase_headers()
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_watchlist"
    params = {"select": "name", "user_id": f"eq.{user_id}"}

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

    normalized: list[dict] = []
    for row in rows:
        name = row.get("name") or row.get("stock_name") or ""
        name = str(name).strip() if name is not None else ""
        # Database currently has no code column; keep code empty to avoid previous 42703 error.
        normalized.append({"name": name, "code": ""})
    logger.info(f"   用户 {user_id} 有 {len(normalized)} 只自选股（仅 name 字段）")
    return normalized


# -------------------- akshare compatibility + retry layer --------------------
def _ak_call_with_fallback(ak_module, candidate_names, *args, retries=3, backoff=1, **kwargs):
    """
    Try function names in candidate_names on ak_module with transient retry/backoff.
    Return the first successful result or None.
    """
    if not ak_module:
        return None

    for name in candidate_names:
        func = getattr(ak_module, name, None)
        if not callable(func):
            continue
        attempt = 0
        while attempt < retries:
            try:
                return func(*args, **kwargs)
            except (RemoteDisconnected, RequestsConnectionError) as e:
                attempt += 1
                wait = backoff * attempt
                logger.warning(f"调用 akshare.{name} 时网络错误（{attempt}/{retries}），重试 {wait}s: {e}")
                time.sleep(wait)
            except Exception as e:
                logger.debug(f"调用 akshare.{name} 抛出异常，跳出重试并尝试下一个候选函数: {e}")
                break
    return None


def get_stock_news(stock_codes: list, days: int = 1) -> list[dict]:
    """Get news for given stock codes using akshare. Returns [] if unavailable."""
    ak = _import_akshare()
    if not ak:
        return []

    all_news = []
    news_candidates = ["stock_news_em", "stock_news", "stock_news_by_code"]
    for code in (stock_codes or [])[:5]:
        try:
            news = _ak_call_with_fallback(ak, news_candidates, symbol=code)
            if news is None:
                continue
            if hasattr(news, "empty") and news.empty:
                continue
            news_list = news.head(10).to_dict("records") if hasattr(news, "head") else list(news)[:10]
            for item in news_list:
                title = item.get("新闻标题") or item.get("title") or item.get("news_title") or ""
                time_str = item.get("发布时间") or item.get("time") or ""
                all_news.append({"title": title, "time": time_str, "source": "东方财富", "stock": code})
        except Exception as e:
            logger.warning(f"获取 {code} 新闻失败: {e}")
            continue
    return all_news[:30]


def get_market_news_summary() -> list[dict]:
    """Get market news summary using akshare. Return [] if unavailable."""
    ak = _import_akshare()
    if not ak:
        return []

    try:
        news = _ak_call_with_fallback(ak, ["stock_news_em", "stock_news"], symbol="000001")
        if not news or (hasattr(news, "empty") and news.empty):
            return []
        recent = news.head(15).to_dict("records") if hasattr(news, "head") else list(news)[:15]
        out = []
        for item in recent:
            title = item.get("新闻标题") or item.get("title") or ""
            time_str = item.get("发布时间") or item.get("time") or ""
            out.append({"title": title, "time": time_str})
        return out[:20]
    except Exception as e:
        logger.warning(f"获取市场新闻失败: {e}")
        return []


def get_stock_quote(stock_code: str) -> dict | None:
    """Get a single stock quote. Return None if not available."""
    ak = _import_akshare()
    if not ak:
        return None

    candidates = ["stock_zh_a_spot_em", "stock_zh_a_spot", "stock_zh_spot"]
    df = _ak_call_with_fallback(ak, candidates)
    if df is None:
        return None

    try:
        if hasattr(df, "empty") and not df.empty:
            if "代码" in df.columns:
                data = df[df["代码"] == stock_code]
            elif "symbol" in df.columns:
                data = df[df["symbol"] == stock_code]
            else:
                data = df[df.iloc[:, 0] == stock_code]
            if not data.empty:
                row = data.iloc[0]
                return {
                    "code": stock_code,
                    "name": row.get("名称", "") or row.get("name", ""),
                    "price": row.get("最新价", 0),
                    "change": row.get("涨跌幅", 0),
                    "volume": row.get("成交量", 0),
                    "amount": row.get("成交额", 0),
                    "high": row.get("最高", 0),
                    "low": row.get("最低", 0),
                    "open": row.get("今开", 0),
                    "yesterday_close": row.get("昨收", 0),
                }
    except Exception as e:
        logger.warning(f"解析行情数据失败: {e}")
        return None
    return None


def get_market_index() -> dict:
    """Return dict of indices (sh, sz, cyb). Empty dict on failure."""
    ak = _import_akshare()
    if not ak:
        return {}

    try:
        idx_df = _ak_call_with_fallback(ak, ["index_zh_a_spot_em", "index_zh_a_spot", "index_zh_spot"])
        if idx_df is None or (hasattr(idx_df, "empty") and idx_df.empty):
            return {}
        def row_for(code):
            if "代码" in idx_df.columns:
                d = idx_df[idx_df["代码"] == code]
            elif "code" in idx_df.columns:
                d = idx_df[idx_df["code"] == code]
            else:
                d = idx_df[idx_df.iloc[:, 0] == code]
            return d.iloc[0] if not d.empty else None

        indices = {}
        sh = row_for("000001")
        if sh is not None:
            indices["sh"] = {"name": "上证指数", "code": "000001", "price": sh.get("最新价", 0), "change": sh.get("涨跌幅", 0)}
        sz = row_for("399001")
        if sz is not None:
            indices["sz"] = {"name": "深证成指", "code": "399001", "price": sz.get("最新价", 0), "change": sz.get("涨跌幅", 0)}
        cyb = row_for("399006")
        if cyb is not None:
            indices["cyb"] = {"name": "创业板指", "code": "399006", "price": cyb.get("最新价", 0), "change": cyb.get("涨跌幅", 0)}
        return indices
    except Exception as e:
        logger.warning(f"获取指数行情失败: {e}")
        return {}


# -------------------- AI content generation for each report --------------------
def generate_morning_brief_ai(user_id: str, watchlist: list) -> str:
    logger.info(f"为用户 {str(user_id)[:12]}... 生成早市简报")
    try:
        market_news = get_market_news_summary()
        stock_codes = [s.get("code", "") for s in watchlist if s.get("code")]
        stock_news = get_stock_news(stock_codes)

        stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"
        news_context = ""
        if market_news:
            news_context += "\n【市场新闻】\n"
            for n in market_news[:10]:
                news_context += f"- {n['title']}\n"
        if stock_news:
            news_context += "\n���自选股相关新闻】\n"
            for n in stock_news[:10]:
                news_context += f"- [{n['stock']}] {n['title']}\n"

        prompt = f"""
你是一位专业的股市分析师。请根据以下信息，为用户生成一份个性化的早市简报（约500-800字）。

用户自选股票：{stock_list}

{news_context}

请按以下结构生成内容（用HTML格式）：
1. 市场回顾（2-3句话）
2. 重点新闻解读（挑选3-5条）
3. 自选股关注
4. 今日展望
5. 操作建议

使用 HTML 标签（<p>、<strong>、<ul>、<li>）进行格式化。
"""
        ai_content = generate_ai_content(prompt)
        if ai_content:
            return ai_content
        return generate_default_morning_brief(watchlist)
    except Exception as e:
        logger.error(f"生成早市简报失败: {e}")
        return generate_default_morning_brief(watchlist)


def generate_midday_review_ai(user_id: str, watchlist: list) -> str:
    logger.info(f"为用户 {str(user_id)[:12]}... 生成中市回顾")
    try:
        indices = get_market_index()
        stock_quotes = []
        for stock in watchlist[:10]:
            quote = get_stock_quote(stock.get("code", ""))
            if quote:
                stock_quotes.append(quote)

        stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"
        market_context = "\n【上午市场表现】\n"
        for key, idx in indices.items():
            try:
                change = float(idx.get("change", 0))
            except Exception:
                change = 0
            direction = "上涨" if change > 0 else "下跌"
            market_context += f"- {idx.get('name')}: {direction} {abs(change):.2f}%\n"

        stocks_context = "\n【自选股表现】\n"
        for q in stock_quotes:
            try:
                change = float(q.get("change", 0))
            except Exception:
                change = 0
            stocks_context += f"- {q.get('name')} : {('上涨' if change > 0 else '下跌')} {abs(change):.2f}%, 价格: {q.get('price')}\n"

        prompt = f"""
你是一位专业的股市分析师。请根据以下上午市场数据，为用户生成一份中市回顾报告（约500-800字）。

用户自选股票：{stock_list}

{market_context}
{stocks_context}

请用 HTML 格式输出，并在结尾给出午后关注点和简短操作建议。
"""
        ai_content = generate_ai_content(prompt)
        if ai_content:
            return ai_content
        return generate_default_midday_review(watchlist)
    except Exception as e:
        logger.error(f"生成中市回顾失败: {e}")
        return generate_default_midday_review(watchlist)


def generate_eod_summary_ai(user_id: str, watchlist: list) -> str:
    logger.info(f"为用户 {str(user_id)[:12]}... 生成尾市总结")
    try:
        indices = get_market_index()
        stock_quotes = []
        for stock in watchlist[:10]:
            quote = get_stock_quote(stock.get("code", ""))
            if quote:
                stock_quotes.append(quote)

        stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"
        market_context = "\n【今日收盘数据】\n"
        for key, idx in indices.items():
            try:
                change = float(idx.get("change", 0))
            except Exception:
                change = 0
            market_context += f"- {idx.get('name')}: {('上涨' if change>0 else '下跌')} {abs(change):.2f}%\n"

        stock_quotes_sorted = sorted(stock_quotes, key=lambda x: float(x.get("change", 0) or 0), reverse=True)
        stocks_context = "\n【自选股今日表现】\n"
        if stock_quotes_sorted:
            top_gainers = stock_quotes_sorted[:3]
            top_losers = stock_quotes_sorted[-3:]
            stocks_context += "\n涨幅榜前三：\n"
            for q in top_gainers:
                stocks_context += f"- {q.get('name')}: +{float(q.get('change',0)):.2f}%\n"
            stocks_context += "\n跌幅榜前三：\n"
            for q in reversed(top_losers):
                stocks_context += f"- {q.get('name')}: {float(q.get('change',0)):.2f}%\n"

        prompt = f"""
你是一位专业的股市分析师。请根据以下今日收盘数据，为用户生成一份尾市总结（约600-900字）。

用户自选股票：{stock_list}

{market_context}
{stocks_context}

请用 HTML 格式输出，包含今日回顾、盘面分析、资金流向、明日展望和操作建议。
"""
        ai_content = generate_ai_content(prompt)
        if ai_content:
            return ai_content
        return generate_default_eod_summary(watchlist)
    except Exception as e:
        logger.error(f"生成尾市总结失败: {e}")
        return generate_default_eod_summary(watchlist)


# -------------------- Default fallback content --------------------
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


# -------------------- Email creation & sending --------------------
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
    """Send email via SMTP (Resend). Return True on success."""
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


# -------------------- Main orchestration --------------------
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

        logger.info("   使用AI生成个性化内容...")
        if report_type == "morning_brief":
            content = generate_morning_brief_ai(user_id, watchlist)
        elif report_type == "midday_review":
            content = generate_midday_review_ai(user_id, watchlist)
        elif report_type == "eod_summary":
            content = generate_eod_summary_ai(user_id, watchlist)
        else:
            logger.error(f"未知的报���类型: {report_type}")
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
    logger.info(f"任��完成: 成功 {success_count}, 失败 {failed_count}")
    logger.info("=" * 60)


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
        print("  python email_system.py morning_brief")
        print("  python email_system.py midday_review")
        print("  python email_system.py eod_summary")
        print("")
        print("请使用环境变量配置敏感密钥（推荐）：SUPABASE_SERVICE_KEY, SUPABASE_URL, RESEND_API_KEY, ZHIPUAI_API_KEY")
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
