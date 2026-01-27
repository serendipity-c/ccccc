#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI驱动邮件发送系统 - 集成版
包含所有邮件发送、AI内容生成、数据获取功能
"""

import os
import sys
import smtplib
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
import logging

# ==================== 配置区 - 所有API密钥集中配置 ====================
# 建议：将这些敏感值改为从环境变量读取（更安全），例如：
# RESEND_API_KEY = os.getenv('RESEND_API_KEY') or '...'
RESEND_API_KEY = 're_Nm5shWrw_4Xp8c94P9VFQ12SC7BxEuuv7'
SMTP_HOST = 'smtp.resend.com'
SMTP_PORT = 587
SMTP_USER = 'resend'
FROM_NAME = 'Portfolio Guardian'
FROM_EMAIL = 'noreply@chenzhaoqi.asia'

# Supabase (数据库)
SUPABASE_URL = 'https://ayjxvejaztusajdntbkh.supabase.co'
SUPABASE_SERVICE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF5anh2ZWphenR1c2FqZG50YmtoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQ0ODAxMSwiZXhwIjoyMDg0MDI0MDExfQ.2Ebe2Ft1gPEfyem0Qie9fGaQ8P3uhJvydGBFyCkvIgE'

# 智谱AI (内容生成)
ZHIPUAI_API_KEY = '21f9ca7cfa0d44f4afeed5ed9d083b23.4zxzk7cZBhr0wnz7'

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ==================== 智谱AI模块 ====================

def get_zhipu_client():
    """获取智谱AI客户端"""
    if not ZHIPUAI_API_KEY:
        logger.warning("未设置 ZHIPUAI_API_KEY")
        return None
    try:
        from zhipuai import ZhipuAI
        return ZhipuAI(api_key=ZHIPUAI_API_KEY)
    except ImportError:
        logger.warning("zhipuai 未安装")
        return None
    except Exception as e:
        logger.error(f"初始化智谱AI客户端失败: {e}")
        return None


def generate_ai_content(prompt: str) -> str:
    """使用智谱AI生成内容"""
    try:
        client = get_zhipu_client()
        if not client:
            return None

        logger.info("正在调用智谱AI生成内容...")

        response = client.chat.completions.create(
            model="glm-4-flash",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2000
        )

        content = response.choices[0].message.content
        logger.info("AI内容生成成功")
        return content

    except Exception as e:
        logger.error(f"AI生成内容失败: {e}")
        return None


# ==================== 数据库模块 ====================

def get_user_id_by_email(email: str):
    """
    根据邮箱从常见的用户表中查找 user_id（兼容多种 schema）
    会尝试 'users', 'user_profiles', 'profiles' 三种表名，并尝试读取常见字段 user_id 或 id
    返回 user_id 字符串或 None
    """
    try:
        headers = {
            'apikey': SUPABASE_SERVICE_KEY,
            'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
            'Content-Type': 'application/json'
        }

        candidate_tables = ['users', 'user_profiles', 'profiles']
        for table in candidate_tables:
            url = f'{SUPABASE_URL}/rest/v1/{table}'
            # 先尝试查 user_id 字段
            params = {
                'select': 'user_id',
                'email': f'eq.{email}'
            }
            logger.debug(f"尝试从表 {table} 获取 user_id，URL={url}, params={params}")
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=10)
            except Exception as e:
                logger.warning(f"请求表 {table} 时出错: {e}")
                continue

            if resp.status_code == 200:
                data = resp.json()
                if data:
                    first = data[0]
                    uid = first.get('user_id') or first.get('id') or first.get('user_id')
                    if uid:
                        logger.info(f"通过表 {table} 找到 user_id: {uid} 对应 email: {email}")
                        return uid
                    # 如果返回了其他字段，尝试取第一个字段的值
                    if len(first) > 0:
                        # 取第一个 value
                        for v in first.values():
                            if v:
                                logger.info(f"通过表 {table} 找到可能的 user_id 值: {v} 对应 email: {email}")
                                return v
                else:
                    # 200 返回但为空，说明在该表中找不到
                    logger.debug(f"表 {table} 返回空结果（未找到该 email）")
                    continue
            elif resp.status_code == 404:
                # 表不存在，跳过
                logger.debug(f"表 {table} 不存在 (404)，跳过")
                continue
            else:
                # 其它错误，记录返回体以便诊断
                logger.warning(f"从表 {table} 查询 user_id 返回状态 {resp.status_code}: {resp.text}")
                continue

        logger.warning(f"未能在候选表中找到 email={email} 对应的 user_id")
        return None

    except Exception as e:
        logger.error(f"get_user_id_by_email 出错: {e}")
        return None


def get_users_with_email_enabled(report_type: str = 'morning_brief'):
    """获取启用了特定邮件的用户（并尝试解析 user_id）"""
    try:
        logger.info(f"查询启用了 {report_type} 的用户...")

        headers = {
            'apikey': SUPABASE_SERVICE_KEY,
            'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
            'Content-Type': 'application/json'
        }

        url = f'{SUPABASE_URL}/rest/v1/user_email_preferences'
        params = {
            'select': '*',
            'enabled': 'eq.true',
            # 这行是针对 JSONB 列中按键过滤（如果表结构是这种格式）
            f'{report_type}->>enabled': 'eq.true'
        }

        logger.info(f"请求 Supabase: GET {url} params={params}")
        response = requests.get(url, params=params, headers=headers, timeout=10)

        if response.status_code != 200:
            logger.error(f"查询失败: {response.status_code} - {response.text}")
            return []

        data = response.json()
        logger.info(f"   找到 {len(data)} 个启用的用户条目")

        # 对每条记录，确保带上 user_id（通过 email 解析）
        enhanced = []
        for record in data:
            email = record.get('email') or record.get('contact')  # 兼容字段名
            user_id = record.get('user_id') or None
            if not user_id and email:
                user_id = get_user_id_by_email(email)
            # 将 user_id 附加回记录，便于后续使用
            record['resolved_user_id'] = user_id or ''
            enhanced.append(record)

        return enhanced

    except Exception as e:
        logger.error(f"获取用户列表失败: {e}")
        return []


def get_user_watchlist(user_id: str):
    """从数据库获取用户自选股票列表（根据 user_id，从 user_watchlist 表获取 name 和 code）"""
    try:
        if not user_id:
            logger.debug("get_user_watchlist: user_id 为空，直接返回空列表")
            return []

        headers = {
            'apikey': SUPABASE_SERVICE_KEY,
            'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
            'Content-Type': 'application/json'
        }

        # 按照你的要求使用单数表名 user_watchlist，并只取 name 字段（及 code 以便后续使用）
        url = f'{SUPABASE_URL}/rest/v1/user_watchlist'
        params = {
            'select': 'name,code',
            'user_id': f'eq.{user_id}'
        }

        logger.info(f"请求 Supabase: GET {url} params={params}")
        response = requests.get(url, params=params, headers=headers, timeout=10)

        if response.status_code != 200:
            logger.error(f"查询自选股失败: {response.status_code} - {response.text}")
            return []

        data = response.json()
        logger.info(f"   用户 {user_id} 有 {len(data)} 只自选股")
        return data

    except Exception as e:
        logger.error(f"获取自选股失败: {e}")
        return []


# ==================== 数据获取模块 ====================

def get_stock_news(stock_codes: list, days: int = 1):
    """获取股票相关新闻（使用东方财富API）"""
    try:
        import akshare as ak
        all_news = []

        for code in stock_codes[:5]:
            try:
                news = ak.stock_news_em(symbol=code)
                if not news.empty:
                    news_list = news.head(10).to_dict('records')
                    for item in news_list:
                        all_news.append({
                            'title': item.get('新闻标题', ''),
                            'time': item.get('发布时间', ''),
                            'source': '东方财富',
                            'stock': code
                        })
            except Exception as e:
                logger.warning(f"获取 {code} 新闻失败: {e}")
                continue

        return all_news[:30]

    except ImportError:
        logger.warning("akshare 未安装")
        return []
    except Exception as e:
        logger.error(f"获取新闻失败: {e}")
        return []


def get_market_news_summary():
    """获取市场��体新闻摘要"""
    try:
        import akshare as ak
        news_summary = []

        try:
            news = ak.stock_news_em(symbol="000001")
            if not news.empty:
                recent_news = news.head(15).to_dict('records')
                for item in recent_news:
                    news_summary.append({
                        'title': item.get('新闻标题', ''),
                        'time': item.get('发布时间', '')
                    })
        except Exception as e:
            logger.warning(f"获取市场新闻失败: {e}")

        return news_summary[:20]

    except ImportError:
        logger.warning("akshare 未安装")
        return []
    except Exception as e:
        logger.error(f"获取市场新闻失败: {e}")
        return []


def get_stock_quote(stock_code: str):
    """获取个股实时行情"""
    try:
        import akshare as ak

        quote = ak.stock_zh_a_spot_em()
        stock_data = quote[quote['代码'] == stock_code]

        if not stock_data.empty:
            row = stock_data.iloc[0]
            return {
                'code': stock_code,
                'name': row.get('名称', ''),
                'price': row.get('最新价', 0),
                'change': row.get('涨跌幅', 0),
                'volume': row.get('成交量', 0),
                'amount': row.get('成交额', 0),
                'high': row.get('最高', 0),
                'low': row.get('最低', 0),
                'open': row.get('今开', 0),
                'yesterday_close': row.get('昨收', 0)
            }
        return None

    except ImportError:
        logger.warning("akshare 未安装")
        return None
    except Exception as e:
        logger.error(f"获取行情失败: {e}")
        return None


def get_market_index():
    """获取主要指数行情"""
    try:
        import akshare as ak
        indices = {}

        try:
            sz_index = ak.index_zh_a_spot_em()

            sh_data = sz_index[sz_index['代码'] == '000001']
            if not sh_data.empty:
                indices['sh'] = {
                    'name': '上证指数',
                    'code': '000001',
                    'price': sh_data.iloc[0].get('最新价', 0),
                    'change': sh_data.iloc[0].get('涨跌幅', 0)
                }

            sz_data = sz_index[sz_index['代码'] == '399001']
            if not sz_data.empty:
                indices['sz'] = {
                    'name': '深证成指',
                    'code': '399001',
                    'price': sz_data.iloc[0].get('最新价', 0),
                    'change': sz_data.iloc[0].get('涨跌幅', 0)
                }

            cyb_data = sz_index[sz_index['代码'] == '399006']
            if not cyb_data.empty:
                indices['cyb'] = {
                    'name': '创业板指',
                    'code': '399006',
                    'price': cyb_data.iloc[0].get('最新价', 0),
                    'change': cyb_data.iloc[0].get('涨跌幅', 0)
                }

        except Exception as e:
            logger.warning(f"获取指数行情失败: {e}")

        return indices

    except Exception as e:
        logger.error(f"获取指数失败: {e}")
        return {}


# ==================== AI内容生成模块 ====================

def generate_morning_brief_ai(user_id: str, watchlist: list) -> str:
    """生成早市简报AI内容（9点）"""
    try:
        logger.info(f"为用户 {str(user_id)[:12]}... 生成早市简报")

        # 获取新闻数据
        market_news = get_market_news_summary()
        stock_codes = [s.get('code') for s in watchlist if s.get('code')]
        stock_news = get_stock_news(stock_codes)

        # 构建AI提示词
        stock_list = ", ".join([f"{s.get('name', '')}({s.get('code', '')})" for s in watchlist[:5]])

        news_context = ""
        if market_news:
            news_context += "\n【市场新闻】\n"
            for news in market_news[:10]:
                news_context += f"- {news['title']}\n"

        if stock_news:
            news_context += "\n【自选股相关新闻】\n"
            for news in stock_news[:10]:
                news_context += f"- [{news['stock']}] {news['title']}\n"

        prompt = f"""
你是一位专业的股市分析师。请根据以下信息，为用户生成一份个性化的早市简报（约500-800字）。

用户自选股票：{stock_list}

{news_context}

请按以下结构生成内容（用HTML格式）：
...
"""

        ai_content = generate_ai_content(prompt)

        if ai_content:
            return ai_content
        else:
            return generate_default_morning_brief(watchlist)

    except Exception as e:
        logger.error(f"生成早市简报失败: {e}")
        return generate_default_morning_brief(watchlist)


def generate_midday_review_ai(user_id: str, watchlist: list) -> str:
    """生成中市回顾AI内容（12点）"""
    try:
        logger.info(f"为用户 {str(user_id)[:12]}... 生成中市回顾")

        indices = get_market_index()
        stock_quotes = []
        for stock in watchlist[:10]:
            quote = get_stock_quote(stock.get('code', ''))
            if quote:
                stock_quotes.append(quote)

        stock_list = ", ".join([f"{s.get('name', '')}({s.get('code', '')})" for s in watchlist[:5]])

        market_context = "\n【上午市场表现】\n"
        for key, index in indices.items():
            direction = "上涨" if index['change'] > 0 else "下跌"
            market_context += f"- {index['name']}: {direction} {abs(index['change']):.2f}%\n"

        stocks_context = "\n【自选股表现】\n"
        for quote in stock_quotes:
            direction = "上涨" if quote['change'] > 0 else "下跌"
            stocks_context += f"- {quote['name']}({quote['code']}): {direction} {abs(quote['change']):.2f}%, 价格: {quote['price']}\n"

        prompt = f"""
你是一位专业的股市分析师。请根据以下上午市场数据，为用户生成一份中市回顾报告（约500-800字）。

用户自选股票：{stock_list}

{market_context}
{stocks_context}

请按以下结构生成内容（用HTML格式）：
...
"""

        ai_content = generate_ai_content(prompt)

        if ai_content:
            return ai_content
        else:
            return generate_default_midday_review(watchlist)

    except Exception as e:
        logger.error(f"生成中市回顾失败: {e}")
        return generate_default_midday_review(watchlist)


def generate_eod_summary_ai(user_id: str, watchlist: list) -> str:
    """生成尾市总结AI内容（4点半）"""
    try:
        logger.info(f"为用户 {str(user_id)[:12]}... 生成尾市总结")

        indices = get_market_index()
        stock_quotes = []
        for stock in watchlist[:10]:
            quote = get_stock_quote(stock.get('code', ''))
            if quote:
                stock_quotes.append(quote)

        stock_list = ", ".join([f"{s.get('name', '')}({s.get('code', '')})" for s in watchlist[:5]])

        market_context = "\n【今日收盘数据】\n"
        for key, index in indices.items():
            direction = "上涨" if index['change'] > 0 else "下跌"
            market_context += f"- {index['name']}: {direction} {abs(index['change']):.2f}%\n"

        stock_quotes_sorted = sorted(stock_quotes, key=lambda x: x['change'], reverse=True)

        stocks_context = "\n【自选股今日表现】\n"
        if stock_quotes_sorted:
            top_gainers = stock_quotes_sorted[:3]
            top_losers = stock_quotes_sorted[-3:]

            stocks_context += "\n涨幅榜前三：\n"
            for quote in top_gainers:
                stocks_context += f"- {quote['name']}: +{quote['change']:.2f}%\n"

            stocks_context += "\n跌幅榜前三：\n"
            for quote in reversed(top_losers):
                stocks_context += f"- {quote['name']}: {quote['change']:.2f}%\n"

        prompt = f"""
你是一位专业的股市分析师。请根据以下今日收盘数据，为用户生成一份尾市总结报告（约600-900字）。

用户自选股票：{stock_list}

{market_context}
{stocks_context}

请按以下结构生成内容（用HTML格式）：
...
"""

        ai_content = generate_ai_content(prompt)

        if ai_content:
            return ai_content
        else:
            return generate_default_eod_summary(watchlist)

    except Exception as e:
        logger.error(f"生成尾市总结失败: {e}")
        return generate_default_eod_summary(watchlist)


# ==================== 默认内容生成函数（备用） ====================
def generate_default_morning_brief(watchlist: list) -> str:
    """生成默认早市简报（AI调用失败时使用）"""
    stock_list = ", ".join([f"{s.get('name', '')}" for s in watchlist[:5]])

    return f"""
    <h2 style="margin: 0 0 16px 0; color: #333;">📅 早市简报</h2>
    ...
    """


def generate_default_midday_review(watchlist: list) -> str:
    """生成默认中市回顾（AI调用失败时使用）"""
    return f"""
    <h2 style="margin: 0 0 16px 0; color: #333;">☀️ 中市回顾</h2>
    ...
    """


def generate_default_eod_summary(watchlist: list) -> str:
    """生成默认尾市总结（AI调用失败时使用）"""
    return f"""
    <h2 style="margin: 0 0 16px 0; color: #333;">🌙 尾市总结</h2>
    ...
    """


# ==================== 邮件发送模块 ====================

def create_simple_html(title: str, content: str) -> str:
    """创建简单的 HTML 邮件"""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{title}</title>
    </head>
    <body style="margin: 0; padding: 0; font-family: Arial, sans-serif; background-color: #f4f4f4;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #f4f4f4;">
            <tr>
                <td style="padding: 40px 0;">
                    <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="margin: 0 auto; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                        <tr>
                            <td style="padding: 30px; border-bottom: 2px solid #667eea;">
                                <h1 style="margin: 0; color: #333; font-size: 24px;">{title}</h1>
                            </td>
                        </tr>
                        <tr>
                            <td style="padding: 30px;">
                                {content}
                            </td>
                        </tr>
                        <tr>
                            <td style="padding: 20px; border-top: 1px solid #e5e7eb; text-align: center; color: #999; font-size: 12px;">
                                此邮件由 Portfolio Guardian 自动发送，请勿直接回复
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """


def send_email(to_email: str, subject: str, html_content: str) -> bool:
    """发送邮件"""
    try:
        logger.info(f"准备发送邮件到: {to_email}")
        logger.info(f"   主题: {subject}")

        # 创建邮件
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = formataddr((FROM_NAME, FROM_EMAIL))
        msg['To'] = to_email

        # 添加 HTML 内容
        html_part = MIMEText(html_content, 'html', 'utf-8')
        msg.attach(html_part)

        # 连接到 SMTP 服务器
        logger.info(f"   连接到 SMTP 服务器: {SMTP_HOST}:{SMTP_PORT}")

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
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


# ==================== 主发送函数 ====================

def send_report(report_type: str):
    """
    发送指定类型的报告（使用AI生成个性化内容）

    Args:
        report_type: 报告类型 ('morning_brief', 'midday_review', 'eod_summary')
    """
    logger.info("=" * 60)

    report_names = {
        'morning_brief': '早市简报',
        'midday_review': '中市回顾',
        'eod_summary': '尾市总结'
    }

    title_prefixes = {
        'morning_brief': '📅 早市简报',
        'midday_review': '☀️ 中市回顾',
        'eod_summary': '🌙 尾市总结'
    }

    logger.info(f"开始执行：{report_names.get(report_type, report_type)}")
    logger.info("=" * 60)

    try:
        # 获取启用的用户列表（现在每条记录带 resolved_user_id）
        users = get_users_with_email_enabled(report_type)

        if not users:
            logger.warning("没有启用的用户，任务结束")
            return

        logger.info(f"找到 {len(users)} 个启用的用户")

        # 统计
        success_count = 0
        failed_count = 0

        title_prefix = title_prefixes.get(report_type, '📊 股市报告')

        # 为每个用户发送个性化邮件
        for user in users:
            email = user.get('email') or user.get('contact') or ''
            user_id = user.get('resolved_user_id', '')  # 使用解析后的 user_id 字段

            logger.info(f"\n处理用户: email={email}, user_id={user_id}")
            logger.info(f"   邮箱: {email}")

            if not email:
                logger.warning("   用户没有设置邮箱，跳过")
                failed_count += 1
                continue

            # 获取用户自选股（按 user_id）
            logger.info("   获取用户自选股...")
            watchlist = get_user_watchlist(user_id)
            logger.info(f"   找到 {len(watchlist)} 只自选股")

            # 使用AI生成个性化内容
            logger.info("   使用AI生成个性化内容...")
            if report_type == 'morning_brief':
                content = generate_morning_brief_ai(user_id, watchlist)
            elif report_type == 'midday_review':
                content = generate_midday_review_ai(user_id, watchlist)
            elif report_type == 'eod_summary':
                content = generate_eod_summary_ai(user_id, watchlist)
            else:
                logger.error(f"未知的报告类型: {report_type}")
                failed_count += 1
                continue

            # 生成完整 HTML
            html = create_simple_html(title_prefix, content)

            # 邮件主题
            today = datetime.now().strftime('%Y年%m月%d日 %A')
            subject = f"{title_prefix} - {today}"

            # 发送邮件
            if send_email(email, subject, html):
                success_count += 1
            else:
                failed_count += 1

        # 输出统计
        logger.info("\n" + "=" * 60)
        logger.info(f"任务完成: 成功 {success_count}, 失败 {failed_count}")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"执行失败: {e}")
        sys.exit(1)


# ==================== 命令行入口 ====================

def main():
    """主函数 - 命令行入口"""
    if len(sys.argv) < 2:
        print("用法: python email_system.py <report_type>")
        print("")
        print("报告类型:")
        print("  morning_brief  - 早市简报 (9:00)")
        print("  midday_review  - 中市回顾 (12:00)")
        print("  eod_summary    - 尾市总结 (16:30)")
        print("")
        print("示例:")
        print("  python email_system.py morning_brief")
        print("  python email_system.py midday_review")
        print("  python email_system.py eod_summary")
        print("")
        print("配置说明:")
        print("  所有API密钥都在代码顶部的配置区")
        print("  建议将敏感密钥放入环境变量并在此处读取")
        sys.exit(1)

    report_type = sys.argv[1].lower()

    valid_types = ['morning_brief', 'midday_review', 'eod_summary']
    if report_type not in valid_types:
        logger.error(f"无效的报告类型: {report_type}")
        logger.error(f"有效类型: {', '.join(valid_types)}")
        sys.exit(1)

    send_report(report_type)


if __name__ == '__main__':
    main()
