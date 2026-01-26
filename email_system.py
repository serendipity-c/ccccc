#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
邮件发送系统 - 单文件集成版本
包含所有邮件发送功能，无需其他依赖模块
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

# 设置环境变量
os.environ['RESEND_API_KEY'] = 're_Nm5shWrw_4Xp8c94P9VFQ12SC7BxEuuv7'
os.environ['SUPABASE_URL'] = 'https://ayjxvejaztusajdntbkh.supabase.co'
os.environ['SUPABASE_SERVICE_KEY'] = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF5anh2ZWphenR1c2FqZG50YmtoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2ODQ0ODAxMSwiZXhwIjoyMDg0MDI0MDExfQ.2Ebe2Ft1gPEfyem0Qie9fGaQ8P3uhJvydGBFyCkvIgE'

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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

        # 获取配置
        smtp_host = 'smtp.resend.com'
        smtp_port = 587
        smtp_user = 'resend'
        resend_api_key = os.getenv('RESEND_API_KEY')
        from_name = 'Portfolio Guardian'
        from_email = 'noreply@chenzhaoqi.asia'

        # 创建邮件
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = formataddr((from_name, from_email))
        msg['To'] = to_email

        # 添加 HTML 内容
        html_part = MIMEText(html_content, 'html', 'utf-8')
        msg.attach(html_part)

        # 连接到 SMTP 服务器
        logger.info(f"   连接到 SMTP 服务器: {smtp_host}:{smtp_port}")

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()  # 启用 TLS
            logger.info("   TLS 已启用")

            # 登录
            server.login(smtp_user, resend_api_key)
            logger.info("   SMTP 登录成功")

            # 发送邮件
            server.send_message(msg)
            logger.info(f"邮件发送成功到 {to_email}")
            return True

    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP 认证失败: {e}")
        logger.error("   请检查 RESEND_API_KEY 是否正确")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"SMTP 错误: {e}")
        return False
    except Exception as e:
        logger.error(f"发送邮件时出错: {e}")
        return False


# ==================== 数据库模块 ====================

def get_users_with_email_enabled(report_type: str = 'morning_brief'):
    """获取启用了特定邮件的用户"""
    try:
        logger.info(f"查询启用了 {report_type} 的用户...")

        url = os.getenv('SUPABASE_URL')
        service_key = os.getenv('SUPABASE_SERVICE_KEY')

        headers = {
            'apikey': service_key,
            'Authorization': f'Bearer {service_key}',
            'Content-Type': 'application/json'
        }

        # 查询启用了邮件的用户
        response = requests.get(
            f'{url}/rest/v1/user_email_preferences',
            params={
                'select': '*',
                'enabled': 'eq.true',
                f'{report_type}->>enabled': 'eq.true'
            },
            headers=headers
        )

        if response.status_code != 200:
            logger.error(f"查询失败: {response.status_code}")
            logger.error(f"   响应: {response.text}")
            return []

        data = response.json()
        logger.info(f"   找到 {len(data)} 个启用的用户")

        return data

    except Exception as e:
        logger.error(f"获取用户列表失败: {e}")
        return []


# ==================== 邮件内容生成模块 ====================

def generate_morning_brief_content() -> str:
    """生成早市简报内容"""
    today = datetime.now().strftime('%Y年%m月%d日 %A')

    content = f"""
    <h2 style="margin: 0 0 16px 0; color: #333;">📅 早市简报 - {today}</h2>

    <div style="margin: 20px 0; padding: 16px; background-color: #f0fdf4; border-left: 4px solid #16a34a; border-radius: 6px;">
        <h3 style="margin: 0 0 8px 0; color: #166534;">✅ 系统运行正常</h3>
        <p style="margin: 0; color: #166534; line-height: 1.6;">
            这是使用 Python smtplib 发送的测试邮件。
        </p>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">📰 市场回顾</h3>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            <strong>前一交易日市场表现：</strong>
        </p>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            • 上证指数收盘涨跌幅：+0.5%
        </p>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            • 深证成指收盘涨跌幅：+0.3%
        </p>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            • 创业板指收盘涨跌幅：+0.8%
        </p>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">🤖 AI 市场预测</h3>
        <p style="margin: 0; color: #666; line-height: 1.6;">
            <strong>整体趋势：</strong><span style="color: #16a34a;">中性偏乐观</span>
        </p>
        <p style="margin: 8px 0; color: #666; line-height: 1.6;">
            <strong>关键点位：</strong>上证指数 支撑3050 / 压力3100
        </p>
        <p style="margin: 8px 0; color: #666; line-height: 1.6;">
            <strong>关注板块：</strong>新能源、半导体、消费、医药
        </p>
        <p style="margin: 8px 0; color: #666; line-height: 1.6;">
            <strong>风险提示：</strong>海外市场波动、政策不确定性
        </p>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">📌 今日关注</h3>
        <ul style="margin: 8px 0; padding-left: 20px; color: #666; line-height: 1.6;">
            <li>关注成交量变化趋势</li>
            <li>北向资金流向</li>
            <li>重点公司公告</li>
            <li>行业政策动态</li>
        </ul>
    </div>

    <div style="margin-top: 24px; padding: 12px; background-color: #fef3c7; border-radius: 4px;">
        <p style="margin: 0; color: #92400e; font-size: 13px;">
            ⏰ 发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </p>
    </div>
    """
    return content


def generate_midday_review_content() -> str:
    """生成中市回顾内容"""
    today = datetime.now().strftime('%Y年%m月%d日 %A')
    current_time = datetime.now().strftime('%H:%M')

    content = f"""
    <h2 style="margin: 0 0 16px 0; color: #333;">☀️ 中市回顾 - {today}</h2>

    <div style="margin: 20px 0; padding: 16px; background-color: #fef3c7; border-left: 4px solid #f59e0b; border-radius: 6px;">
        <h3 style="margin: 0 0 8px 0; color: #92400e;">⏰ 盘中更新</h3>
        <p style="margin: 0; color: #92400e; line-height: 1.6;">
            当前时间：{current_time} | 市场正在进行中
        </p>
    </div>

    <div style="margin: 20px 0; padding: 16px; background-color: #f0fdf4; border-left: 4px solid #16a34a; border-radius: 6px;">
        <h3 style="margin: 0 0 8px 0; color: #166534;">✅ 系统运行正常</h3>
        <p style="margin: 0; color: #166534; line-height: 1.6;">
            这是使用 Python smtplib 发送的中市回顾邮件。
        </p>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">📊 上午市场表现</h3>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            <strong>上证指数：</strong>上涨 0.5% | 成交量 1200亿
        </p>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            <strong>深证成指：</strong>上涨 0.3% | 成交量 1500亿
        </p>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            <strong>创业板指：</strong>上涨 0.8% | 成交量 800亿
        </p>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">🔥 热门板块</h3>
        <ul style="margin: 8px 0; padding-left: 20px; color: #666; line-height: 1.6;">
            <li><strong>新能源</strong> +2.3% - 政策利好持续发酵</li>
            <li><strong>半导体</strong> +1.8% - 国产替代加速</li>
            <li><strong>消费电子</strong> +1.2% - 新品发布预期</li>
            <li><strong>医药生物</strong> -0.5% - 短期调整</li>
        </ul>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">💰 资金流向</h3>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            <strong>北向资金：</strong>净流入 35亿元
        </p>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            <strong>主力资金：</strong>净流出 12亿元
        </p>
        <p style="margin: 8px 0; color: #666; line-height: 1.6;">
            <strong>机构动向：</strong>加仓科技、减仓周期
        </p>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">📌 午后关注</h3>
        <ul style="margin: 8px 0; padding-left: 20px; color: #666; line-height: 1.6;">
            <li>关注成交量能否持续放大</li>
            <li>重点板块的延续性</li>
            <li>尾盘资金流向变化</li>
            <li>港股走势影响</li>
        </ul>
    </div>

    <div style="margin-top: 24px; padding: 12px; background-color: #fef3c7; border-radius: 4px;">
        <p style="margin: 0; color: #92400e; font-size: 13px;">
            ⏰ 发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </p>
    </div>
    """
    return content


def generate_eod_summary_content() -> str:
    """生成尾市总结内容"""
    today = datetime.now().strftime('%Y年%m月%d日 %A')

    content = f"""
    <h2 style="margin: 0 0 16px 0; color: #333;">🌙 尾市总结 - {today}</h2>

    <div style="margin: 20px 0; padding: 16px; background-color: #eff6ff; border-left: 4px solid #3b82f6; border-radius: 6px;">
        <h3 style="margin: 0 0 8px 0; color: #1e40af;">🏁 今日收盘</h3>
        <p style="margin: 0; color: #1e40af; line-height: 1.6;">
            市场已收盘，今日交易结束
        </p>
    </div>

    <div style="margin: 20px 0; padding: 16px; background-color: #f0fdf4; border-left: 4px solid #16a34a; border-radius: 6px;">
        <h3 style="margin: 0 0 8px 0; color: #166534;">✅ 系统运行正常</h3>
        <p style="margin: 0; color: #166534; line-height: 1.6;">
            这是使用 Python smtplib 发送的尾市总结邮件。
        </p>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">📈 今日大盘表现</h3>
        <table style="width: 100%; border-collapse: collapse; margin: 12px 0;">
            <tr style="background-color: #f3f4f6;">
                <th style="padding: 10px; text-align: left; border: 1px solid #e5e7eb;">指数</th>
                <th style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">收盘</th>
                <th style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">涨跌幅</th>
                <th style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">成交量</th>
            </tr>
            <tr>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">上证指数</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">3,085.25</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb; color: #16a34a;">+0.52%</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">2,850亿</td>
            </tr>
            <tr>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">深证成指</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">10,156.33</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb; color: #16a34a;">+0.35%</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">3,420亿</td>
            </tr>
            <tr>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">创业板指</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">2,034.21</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb; color: #16a34a;">+0.78%</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">1,580亿</td>
            </tr>
        </table>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">🎯 今日亮点</h3>
        <ul style="margin: 8px 0; padding-left: 20px; color: #666; line-height: 1.6;">
            <li><strong>三大指数全红收盘</strong> - 市场情绪回暖</li>
            <li><strong>新能源板块领涨</strong> - 政策利好持续发酵</li>
            <li><strong>成交额放量</strong> - 两市合计超 8000亿</li>
            <li><strong>北向资金净流入</strong> - 全天净流入 52亿元</li>
        </ul>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">📊 板块表现</h3>
        <table style="width: 100%; border-collapse: collapse; margin: 12px 0;">
            <tr style="background-color: #f3f4f6;">
                <th style="padding: 10px; text-align: left; border: 1px solid #e5e7eb;">板块</th>
                <th style="padding: 10px; text-align: right; border: 1px solid #e5e7eb;">涨跌幅</th>
                <th style="padding: 10px; text-align: left; border: 1px solid #e5e7eb;">原因</th>
            </tr>
            <tr>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">新能源</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb; color: #16a34a;">+2.8%</td>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">政策支持</td>
            </tr>
            <tr>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">半导体</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb; color: #16a34a;">+2.1%</td>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">国产替代</td>
            </tr>
            <tr>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">房地产</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb; color: #dc2626;">-1.5%</td>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">获利回吐</td>
            </tr>
            <tr>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">银行</td>
                <td style="padding: 10px; text-align: right; border: 1px solid #e5e7eb; color: #dc2626;">-0.8%</td>
                <td style="padding: 10px; border: 1px solid #e5e7eb;">调整压力</td>
            </tr>
        </table>
    </div>

    <div style="margin: 20px 0;">
        <h3 style="margin: 0 0 12px 0; color: #333;">💡 明日展望</h3>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            <strong>技术面：</strong>上证指数站稳 3050 点，有望挑战 3100 点
        </p>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            <strong>资金面：</strong>北向资金持续流入，市场信心恢复
        </p>
        <p style="margin: 0 0 8px 0; color: #666; line-height: 1.6;">
            <strong>关注点：</strong>成交量能否持续放大、政策面动态
        </p>
        <p style="margin: 8px 0; color: #666; line-height: 1.6;">
            <strong>风险提示：</strong>海外市场波动、量能不足风险
        </p>
    </div>

    <div style="margin-top: 24px; padding: 12px; background-color: #fef3c7; border-radius: 4px;">
        <p style="margin: 0; color: #92400e; font-size: 13px;">
            ⏰ 发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </p>
    </div>
    """
    return content


# ==================== 主发送函数 ====================

def send_report(report_type: str):
    """
    发送指定类型的报告

    Args:
        report_type: 报告类型 ('morning_brief', 'midday_review', 'eod_summary')
    """
    logger.info("=" * 60)

    report_names = {
        'morning_brief': '早市简报',
        'midday_review': '中市回顾',
        'eod_summary': '尾市总结'
    }

    logger.info(f"开始执行：{report_names.get(report_type, report_type)}")
    logger.info("=" * 60)

    try:
        # 获取启用的用户列表
        users = get_users_with_email_enabled(report_type)

        if not users:
            logger.warning("没有启用的用户，任务结束")
            return

        logger.info(f"找到 {len(users)} 个启用的用户")

        # 统计
        success_count = 0
        failed_count = 0

        # 生成邮件内容
        if report_type == 'morning_brief':
            content = generate_morning_brief_content()
            title_prefix = '📅 早市简报'
        elif report_type == 'midday_review':
            content = generate_midday_review_content()
            title_prefix = '☀️ 中市回顾'
        elif report_type == 'eod_summary':
            content = generate_eod_summary_content()
            title_prefix = '🌙 尾市总结'
        else:
            logger.error(f"未知的报告类型: {report_type}")
            return

        # 为每个用户发送邮件
        for user in users:
            user_id = user.get('user_id', '')
            email = user.get('email', '')

            logger.info(f"\n处理用户: {user_id[:12]}...")
            logger.info(f"   邮箱: {email}")

            if not email:
                logger.warning("   用户没有设置邮箱，跳过")
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
        print("  morning_brief  - 早市简报")
        print("  midday_review  - 中市回顾")
        print("  eod_summary    - 尾市总结")
        print("")
        print("示例:")
        print("  python email_system.py morning_brief")
        print("  python email_system.py midday_review")
        print("  python email_system.py eod_summary")
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
