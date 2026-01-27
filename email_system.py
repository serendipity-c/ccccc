#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI驱动邮件发送系统 - 使用Supabase Edge Functions获取数据
将数据获取方式从akshare改为调用Supabase Edge Functions（market-data）
"""

from __future__ import annotations

import sys
import time
import smtplib
import requests
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from typing import Any

# -------------------- 全部硬编码配置 --------------------
# Resend (SMTP)
RESEND_API_KEY = "re_Nm5shWrw_4Xp8c94P9VFQ12SC7BxEuuv7"
SMTP_HOST = "smtp.resend.com"
SMTP_PORT = 587
SMTP_USER = "resend"
FROM_NAME = "Portfolio Guardian"
FROM_EMAIL = "noreply@chenzhaoqi.asia"

# Supabase (数据库 + Edge Functions)
SUPABASE_URL = "https://ayjxvejaztusajdntbkh.supabase.co"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF5anh2ZWphenR1c2FqZG50YmtoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQ0ODAxMSwiZXhwIjoyMDg0MDI0MDExfQ.2Ebe2Ft1gPEfyem0Qie9fGaQ8P3uhJvydGBFyCkvIgE"

# 智谱AI (AI 内容生成)
ZHIPUAI_API_KEY = "21f9ca7cfa0d44f4afeed5ed9d083b23.4zxzk7cZBhr0wnz7"

# -------------------- 日志配置 --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -------------------- Supabase 帮助函数 --------------------
def _supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json"
    }

def _supabase_rest_headers():
    """用于查询数据库的headers"""
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json"
    }

# ==================== 数据获取层 - 通过 Supabase Edge Functions ====================

def invoke_edge_function(function_name: str, body: dict) -> dict | None:
    """
    调用 Supabase Edge Function
    function_name: 例如 'market-data'
    body: 请求体，例如 {'action': 'batch_quotes', 'symbols': ['600519.SH']}
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("SUPABASE_URL 或 SUPABASE_SERVICE_KEY 未设置")
        return None

    url = f"{SUPABASE_URL.rstrip('/')}/functions/v1/{function_name}"
    headers = _supabase_headers()

    try:
        logger.debug(f"调用 Edge Function: {function_name} with body={body}")
        resp = requests.post(url, json=body, headers=headers, timeout=30)

        if resp.status_code == 200:
            return resp.json()
        else:
            logger.warning(f"Edge Function 返回错误: {resp.status_code} - {resp.text}")
            return None
    except Exception as e:
        logger.error(f"调用 Edge Function 失败: {e}")
        return None


# -------------------- 股票行情数据 --------------------

def format_ts_code(symbol: str) -> str:
    """
    转换股票代码格式：600519 -> 600519.SH, 00700 -> 00700.HK
    """
    if symbol.endswith('.SH') or symbol.endswith('.SZ') or symbol.endswith('.HK'):
        return symbol

    # 港股：5位数字
    if len(symbol) == 5 and symbol.isdigit():
        return f"{symbol}.HK"
    # A股上海：6或9开头的6位数字
    if len(symbol) == 6 and symbol.isdigit() and (symbol[0] == '6' or symbol[0] == '9'):
        return f"{symbol}.SH"
    # A股深圳：其他6位数字
    if len(symbol) == 6 and symbol.isdigit():
        return f"{symbol}.SZ"

    return symbol


def get_stock_quote(stock_code: str) -> dict | None:
    """
    获取单只股票的实时行情
    通过 Supabase Edge Function 'market-data' 的 batch_quotes 接口
    """
    ts_code = format_ts_code(stock_code)

    data = invoke_edge_function('market-data', {
        'action': 'batch_quotes',
        'symbols': [ts_code]
    })

    if data and data.get('code') == 0:
        quotes = data.get('data', {})
        if ts_code in quotes:
            quote_data = quotes[ts_code]
            if quote_data.get('code') == 0:
                q = quote_data.get('data', {})
                return {
                    "code": stock_code,
                    "name": q.get('name', ''),
                    "price": q.get('price', q.get('close', 0)),
                    "change": q.get('change', 0),
                    "changePercent": q.get('changePercent', q.get('pct_chg', 0)),
                    "volume": q.get('volume', q.get('vol', 0)),
                    "amount": q.get('amount', 0),
                    "high": q.get('high', 0),
                    "low": q.get('low', 0),
                    "open": q.get('open', 0),
                    "yesterday_close": q.get('prevClose', q.get('pre_close', 0)),
                }

    logger.warning(f"未能获取股票 {stock_code} 的行情数据")
    return None


def get_stock_quotes_batch(stock_codes: list[str]) -> dict[str, dict]:
    """
    批量获取股票行情
    返回: {股票代码: 行情数据}
    """
    if not stock_codes:
        return {}

    ts_codes = [format_ts_code(code) for code in stock_codes]

    data = invoke_edge_function('market-data', {
        'action': 'batch_quotes',
        'symbols': ts_codes
    })

    result = {}
    if data and data.get('code') == 0:
        quotes = data.get('data', {})
        for original_code, ts_code in zip(stock_codes, ts_codes):
            if ts_code in quotes:
                quote_data = quotes[ts_code]
                if quote_data.get('code') == 0:
                    q = quote_data.get('data', {})
                    result[original_code] = {
                        "code": original_code,
                        "name": q.get('name', ''),
                        "price": q.get('price', q.get('close', 0)),
                        "change": q.get('change', 0),
                        "changePercent": q.get('changePercent', q.get('pct_chg', 0)),
                        "volume": q.get('volume', q.get('vol', 0)),
                        "amount": q.get('amount', 0),
                        "high": q.get('high', 0),
                        "low": q.get('low', 0),
                        "open": q.get('open', 0),
                        "yesterday_close": q.get('prevClose', q.get('pre_close', 0)),
                    }

    return result


def get_market_index() -> dict:
    """
    获取主要指数行情（上证、深证、创业板）
    通过 Supabase Edge Function 'market-data' 的 index_quotes 接口
    """
    data = invoke_edge_function('market-data', {
        'action': 'index_quotes'
    })

    indices = {}
    if data and data.get('code') == 0:
        index_list = data.get('data', [])
        for idx in index_list:
            code = idx.get('ts_code', '')
            name = idx.get('name', '')
            price = idx.get('close', 0)
            change = idx.get('pct_chg', 0)

            if '000001' in code:  # 上证指数
                indices['sh'] = {
                    'name': name or '上证指数',
                    'code': '000001',
                    'price': price,
                    'change': change
                }
            elif '399001' in code:  # 深证成指
                indices['sz'] = {
                    'name': name or '深证成指',
                    'code': '399001',
                    'price': price,
                    'change': change
                }
            elif '399006' in code:  # 创业板指
                indices['cyb'] = {
                    'name': name or '创业板指',
                    'code': '399006',
                    'price': price,
                    'change': change
                }

    return indices


# -------------------- 新闻数据 --------------------

def get_market_news() -> list[dict]:
    """
    获取市场新闻（财经快讯）
    通过 Supabase Edge Function 'market-data' 的 finance_flash 接口
    """
    data = invoke_edge_function('market-data', {
        'action': 'finance_flash',
        'limit': 20
    })

    news_list = []
    if data and data.get('code') == 0:
        raw_news = data.get('data', [])
        for item in raw_news[:20]:
            news_list.append({
                'title': item.get('title', item.get('digest', '')),
                'time': item.get('showtime', ''),
                'source': '东方财富'
            })

    return news_list


def get_stock_news(stock_codes: list[str], limit: int = 10) -> list[dict]:
    """
    获取股票相关新闻
    通过 Supabase Edge Function 'market-data' 的 news 接口
    """
    if not stock_codes:
        return []

    data = invoke_edge_function('market-data', {
        'action': 'news',
        'category': 'company',
        'limit': limit
    })

    news_list = []
    if data and data.get('code') == 0:
        raw_news = data.get('data', [])
        for item in raw_news[:limit]:
            title = item.get('title', '')
            # 检查是否包含股票代码
            related_stocks = []
            content = title.lower()
            for code in stock_codes[:5]:
                if code in content or format_ts_code(code).replace('.', '') in content:
                    related_stocks.append(code)

            news_list.append({
                'title': title,
                'time': item.get('datetime', item.get('time', '')),
                'source': item.get('source', '财经新闻'),
                'stock': related_stocks[0] if related_stocks else ''
            })

    return news_list


# -------------------- 智谱AI集成 --------------------
def _import_zhipuai():
    """惰性导入 zhipuai"""
    try:
        from zhipuai import ZhipuAI
        return ZhipuAI
    except ImportError:
        logger.warning("zhipuai 未安装 — AI 内容生成功能将被禁用")
        return None
    except Exception as e:
        logger.warning(f"导入 zhipuai 失败: {e}")
        return None


def get_zhipu_client():
    """返回 zhipuai 客户端实例"""
    if not ZHIPUAI_API_KEY:
        logger.warning("未设置 ZHIPUAI_API_KEY")
        return None

    ZhipuAI = _import_zhipuai()
    if not ZhipuAI:
        return None

    try:
        return ZhipuAI(api_key=ZHIPUAI_API_KEY)
    except Exception as e:
        logger.error(f"初始化智谱AI客户端失败: {e}")
        return None


def generate_ai_content(prompt: str) -> str | None:
    """使用智谱AI生成内容"""
    try:
        client = get_zhipu_client()
        if not client:
            return None

        logger.info("正在调用智谱AI生成内容...")
        response = client.chat.completions.create(
            model="glm-4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=2000
        )

        content = None
        if hasattr(response, "choices") and response.choices:
            content = response.choices[0].message.content

        if not content:
            logger.warning("AI 返回空内容")
            return None

        logger.info("AI 内容生成成功")
        return content

    except Exception as e:
        logger.error(f"AI 生成内容失败: {e}")
        return None


# -------------------- Supabase 数据库查询 --------------------

def get_user_id_by_email(email: str) -> str | None:
    """通过常见用户表解析 email 对应的 user_id"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None

    headers = _supabase_rest_headers()
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
                logger.info(f"通过表 {table} 找到 user_id={uid}")
                return str(uid)
        elif resp.status_code == 404:
            continue

    logger.warning(f"未能解析 email={email} 对应的 user_id")
    return None


def get_users_with_email_enabled(report_type: str = "morning_brief") -> list[dict]:
    """查询启用邮件的用户"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return []

    headers = _supabase_rest_headers()
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
        logger.error(f"查询失败: {resp.status_code} - {resp.text}")
        return []

    try:
        records = resp.json()
    except Exception as e:
        logger.error(f"解析响应失败: {e}")
        return []

    logger.info(f"   找到 {len(records)} 个启用的用户")

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
    """获取用户自选股"""
    if not user_id:
        return []

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return []

    headers = _supabase_rest_headers()
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_watchlist"
    params = {"select": "name", "user_id": f"eq.{user_id}"}

    logger.info(f"请求自选股: user_id={user_id}")
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
        logger.error(f"解析响应失败: {e}")
        return []

    normalized = []
    for row in rows:
        name = row.get("name") or row.get("stock_name") or ""
        name = str(name).strip() if name is not None else ""
        normalized.append({"name": name, "code": name})  # name可能包含代码

    logger.info(f"   用户 {user_id} 有 {len(normalized)} 只自选股")
    return normalized


# -------------------- AI 内容生成（按报告类型） --------------------

def generate_morning_brief_ai(user_id: str, watchlist: list) -> str:
    """生成早市简报"""
    logger.info(f"为用户 {str(user_id)[:12]}... 生成早市简报")
    try:
        market_news = get_market_news()
        stock_codes = [s.get("code", "") for s in watchlist if s.get("code")]
        stock_news = get_stock_news(stock_codes)

        stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"

        news_context = ""
        if market_news:
            news_context += "\n【市场新闻】\n"
            for n in market_news[:10]:
                news_context += f"- {n['title']}\n"
        if stock_news:
            news_context += "\n【自选股相关新闻】\n"
            for n in stock_news[:10]:
                news_context += f"- [{n.get('stock', '')}] {n['title']}\n"

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
    """生成中市回顾"""
    logger.info(f"为用户 {str(user_id)[:12]}... 生成中市回顾")
    try:
        indices = get_market_index()
        stock_quotes = []
        stock_codes = [s.get("code", "") for s in watchlist if s.get("code")]

        if stock_codes:
            quotes_batch = get_stock_quotes_batch(stock_codes[:10])
            for code in stock_codes[:10]:
                if code in quotes_batch:
                    stock_quotes.append(quotes_batch[code])

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
                change = float(q.get("changePercent", q.get("change", 0)))
            except Exception:
                change = 0
            direction = "上涨" if change > 0 else "下跌"
            stocks_context += f"- {q.get('name')}: {direction} {abs(change):.2f}%, 价格: {q.get('price')}\n"

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
    """生成尾市总结"""
    logger.info(f"为用户 {str(user_id)[:12]}... 生成尾市总结")
    try:
        indices = get_market_index()
        stock_quotes = []
        stock_codes = [s.get("code", "") for s in watchlist if s.get("code")]

        if stock_codes:
            quotes_batch = get_stock_quotes_batch(stock_codes[:10])
            for code in stock_codes[:10]:
                if code in quotes_batch:
                    stock_quotes.append(quotes_batch[code])

        stock_list = ", ".join([f"{s.get('name','')}" for s in watchlist[:5]]) or "暂无自选股"

        market_context = "\n【今日收盘数据】\n"
        for key, idx in indices.items():
            try:
                change = float(idx.get("change", 0))
            except Exception:
                change = 0
            direction = "上涨" if change > 0 else "下跌"
            market_context += f"- {idx.get('name')}: {direction} {abs(change):.2f}%\n"

        stock_quotes_sorted = sorted(
            stock_quotes,
            key=lambda x: float(x.get("changePercent", x.get("change", 0) or 0)),
            reverse=True
        )

        stocks_context = "\n【自选股今日表现】\n"
        if stock_quotes_sorted:
            top_gainers = stock_quotes_sorted[:3]
            top_losers = stock_quotes_sorted[-3:]
            stocks_context += "\n涨幅榜前三：\n"
            for q in top_gainers:
                chg = float(q.get("changePercent", q.get("change", 0)))
                stocks_context += f"- {q.get('name')}: +{chg:.2f}%\n"
            stocks_context += "\n跌幅榜前三：\n"
            for q in reversed(top_losers):
                chg = float(q.get("changePercent", q.get("change", 0)))
                stocks_context += f"- {q.get('name')}: {chg:.2f}%\n"

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


# -------------------- 邮件创建与发送 --------------------

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
    """通过 SMTP 发送邮件"""
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
    report_names = {
        "morning_brief": "早市简报",
        "midday_review": "中市回顾",
        "eod_summary": "尾市总结"
    }
    title_prefixes = {
        "morning_brief": "📅 早市简报",
        "midday_review": "☀️ 中市回顾",
        "eod_summary": "🌙 尾市总结"
    }

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


def main():
    if len(sys.argv) < 2:
        print("用法: python email_system_v2.py <report_type>")
        print("")
        print("报告类型:")
        print("  morning_brief  - 早市简报 (08:30)")
        print("  midday_review  - 中市回顾 (12:00)")
        print("  eod_summary    - 尾市总结 (16:30)")
        print("")
        print("示例:")
        print("  python email_system_v2.py morning_brief")
        print("  python email_system_v2.py midday_review")
        print("  python email_system_v2.py eod_summary")
        print("")
        print("数据来源: Supabase Edge Functions (market-data)")
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
