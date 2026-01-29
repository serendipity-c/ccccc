#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
邮件系统：读取 Supabase 的用户自选股 -> 启动 Flask Web 服务 & WebStockAnalyzer（若尚未启动）
-> 将自选股放入分析器进行分析 -> 汇总分析结果并通过 SMTP 发邮件给用户

说明：
- 该脚本假设 flask_web_server.py、web_stock_analyzer.py 与 config.json 在同一项目目录中（已由你提供）。
- 脚本会在第一次需要时在后台以线程方式启动 flask_web_server.app（默认监听 0.0.0.0:5000），
  并注入同一实例的 WebStockAnalyzer 到 flask_web_server.analyzer，从而 Web UI 与本脚本共享分析器实例。
- 分析使用 WebStockAnalyzer.analyze_stock(stock_code)；为避免单只股票耗时过��，支持 per-stock 超时（默认 120s）。
- 请确保运行环境已安装 requirements.txt 中所需依赖（尤其 akshare、pandas）。
- 敏感配置（SUPABASE_SERVICE_KEY、SMTP 凭据等）建议改为环境变量或 GitHub Secrets；此处为演示保留硬编码位置。

用法：
  python email_system.py <report_type>
  report_type: morning_brief | midday_review | eod_summary
"""
from __future__ import annotations

import os
import sys
import time
import json
import logging
import threading
import smtplib
import requests
import requirements
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from typing import List

# -------------------- 基本配置（请用 Secrets 替换） --------------------
# SMTP (Resend 示例)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "re_Nm5shWrw_4Xp8c94P9VFQ12SC7BxEuuv7")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.resend.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "resend")
FROM_NAME = os.environ.get("FROM_NAME", "Portfolio Guardian")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "noreply@chenzhaoqi.asia")

# Supabase (数据库)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://ayjxvejaztusajdntbkh.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "eyJhbGciOiJIUzI...")

# Flask / WebStockAnalyzer 服务配置
# If empty, the script will still instantiate WebStockAnalyzer and start Flask on localhost:5000
DR_FLASK_HOST = os.environ.get("DR_FLASK_HOST", "0.0.0.0")
DR_FLASK_PORT = int(os.environ.get("DR_FLASK_PORT", 5000))
PER_STOCK_TIMEOUT = int(os.environ.get("PER_STOCK_TIMEOUT", 120))  # seconds per stock analysis

# -------------------- 日志 --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------- Lazy imports for analyzer/web server --------------------
_flask_thread = None
_flask_started = False
_analyzer_instance = None
_flask_lock = threading.Lock()


def _supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json"
    }


def get_user_id_by_email(email: str) -> str | None:
    """从Supabase中尝试查找用户ID（兼容多个表名）"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.warning("SUPABASE 未配置")
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
    return None


def get_users_with_email_enabled(report_type: str = "morning_brief") -> list[dict]:
    """查询 user_email_preferences 并解析 resolved_user_id"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("Supabase 未配置")
        return []

    headers = _supabase_headers()
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/user_email_preferences"
    params = {
        "select": "*",
        "enabled": "eq.true",
        f"{report_type}->>enabled": "eq.true"
    }
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
    从 Supabase 获取用户自选股（select="*")，兼容只有 name 的情况。
    返回每条记录：{"name","raw_code","code","market"}
    该代码重用之前的解析逻辑（尽量保持兼容）。
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
        # 多来源尝试读取名称
        name = row.get("name") or row.get("stock_name") or ""
        raw_code = (
            row.get("code")
            or row.get("symbol")
            or row.get("stock_code")
            or row.get("ticker")
            or row.get("id")
            or ""
        )
        name = str(name).strip() if name is not None else ""
        raw_code = str(raw_code).strip() if raw_code is not None else ""

        # If only name is available, leave code empty - later we'll try to resolve via analyzer or heuristics
        normalized.append({
            "name": name,
            "raw_code": raw_code,
            "code": raw_code or "",  # may be empty
            "market": ""  # optional, analyzer will handle if needed
        })
    logger.info(f"   用户 {user_id} 有 {len(normalized)} 条自选股（可能仅含 name）")
    return normalized

# -------------------- Flask & WebStockAnalyzer integration --------------------
def ensure_analyzer_and_server(start_flask: bool = True):
    """
    确保全局的 WebStockAnalyzer 实例存在，并在后台启动 flask_web_server.app（如果需要）。
    Returns the analyzer instance.
    """
    global _analyzer_instance, _flask_thread, _flask_started

    # Lazy import WebStockAnalyzer
    if _analyzer_instance is None:
        try:
            from web_stock_analyzer import WebStockAnalyzer
        except Exception as e:
            logger.error(f"无法导入 WebStockAnalyzer: {e}")
            raise

        logger.info("正在实例化 WebStockAnalyzer ... 这可能需要一些时间（会加载 akshare 等依赖）")
        _analyzer_instance = WebStockAnalyzer()  # may raise

    # Start Flask server in background and inject analyzer
    if start_flask:
        with _flask_lock:
            if not _flask_started:
                try:
                    import flask_web_server as fw
                    # Inject the same analyzer instance into flask_web_server module so UI endpoints use it
                    fw.analyzer = _analyzer_instance
                    # Start Flask app in daemon thread
                    def _run_flask():
                        try:
                            logger.info(f"启动 Flask Web 服务器（后台）: {DR_FLASK_HOST}:{DR_FLASK_PORT}")
                            # Note: Using app.run here is OK for background demo/testing; for production use gunicorn.
                            fw.app.run(host=DR_FLASK_HOST, port=DR_FLASK_PORT, debug=False, use_reloader=False, threaded=True)
                        except Exception as e:
                            logger.error(f"Flask 服务器启动失败: {e}")

                    _flask_thread = threading.Thread(target=_run_flask, daemon=True, name="flask_web_server_thread")
                    _flask_thread.start()
                    # Small delay to allow the server to start
                    time.sleep(1.0)
                    _flask_started = True
                    logger.info("Flask Web 服务器已在后台启动（daemon thread）")
                except Exception as e:
                    logger.error(f"无法启动或注入 Flask Web 服务器: {e}")
                    # don't raise; continue with analyzer-only mode
    return _analyzer_instance

# -------------------- Analysis orchestration --------------------
def analyze_watchlist_with_analyzer(analyzer, watchlist: List[dict], per_stock_timeout: int = PER_STOCK_TIMEOUT) -> List[dict]:
    """
    使用传入的 analyzer（WebStockAnalyzer 实例）对 watchlist 中的每只股票做分析。
    watchlist 项结构：{"name","raw_code","code","market"}
    返回每只股票的分析报告（dict），包含 stock_code、stock_name、ai_analysis、scores、recommendation 等
    """
    results = []
    if not analyzer:
        logger.error("Analyzer 未传入")
        return results

    # Build list of candidate codes to analyze; if code empty, try using name as code
    candidates = []
    for s in watchlist:
        code = (s.get("code") or s.get("raw_code") or "").strip()
        name = s.get("name") or ""
        if not code and name:
            # Attempt to extract digits or ticker-like string from name as fallback
            import re
            m = re.search(r'[\(\（\[]\s*([0-9A-Za-z\.\-]{2,10})\s*[\)\）\]]', name)
            if m:
                code = m.group(1)
            else:
                # take first token
                code = name.split()[0]
        if code:
            candidates.append({"requested_name": name, "stock_code": code})
    if not candidates:
        logger.warning("没有可识别的自选股代码，跳过分析")
        return results

    # Use ThreadPoolExecutor to parallelize some analyses but keep limited concurrency
    max_workers = min(4, max(1, len(candidates)))
    logger.info(f"准备对 {len(candidates)} 支自选股进行分析（并发 {max_workers}）")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for c in candidates:
            stock_code = c["stock_code"]
            # Submit analyzer.analyze_stock in a separate thread to allow timeout control
            futures[pool.submit(_safe_analyze_stock, analyzer, stock_code)] = c

        for fut in as_completed(futures, timeout=None):
            ctx = futures[fut]
            stock_code = ctx["stock_code"]
            requested_name = ctx["requested_name"]
            try:
                report = fut.result(timeout=per_stock_timeout)
                # report is the analyzer output dict
                if report:
                    # ensure we have basic keys
                    report.setdefault("stock_code", stock_code)
                    report.setdefault("stock_name", report.get("stock_name") or requested_name or stock_code)
                    results.append(report)
                    logger.info(f"分析完成: {stock_code}")
                else:
                    logger.warning(f"{stock_code} 返回空报告")
            except TimeoutError:
                logger.error(f"{stock_code} 分析超时（>{per_stock_timeout}s），跳过")
            except Exception as e:
                logger.error(f"{stock_code} 分析失败: {e}")
    return results


def _safe_analyze_stock(analyzer, stock_code):
    """封装调用 analyzer.analyze_stock，捕获异常并返回 None 或结果"""
    try:
        return analyzer.analyze_stock(stock_code, enable_streaming=False)
    except Exception as e:
        logger.error(f"analyze_stock 出错 ({stock_code}): {e}")
        return None

# -------------------- Email content builder --------------------
def build_email_html_from_reports(user_watchlist_reports: List[dict], user_info: dict, report_type: str) -> str:
    """
    将多个单股报告合成一份 HTML 邮件正文（简单布局）。
    user_watchlist_reports: list of analyzer report dicts
    """
    header = f"<h2>{report_type} - 针对您的自选股分析报告</h2>"
    if not user_watchlist_reports:
        body = "<p>当前无法获取自选股的详细分析，可能是行情/数据源或AI服务不可用。</p>"
        return header + body

    parts = []
    for r in user_watchlist_reports:
        stock_name = r.get("stock_name") or r.get("stock_code")
        code = r.get("stock_code")
        price = r.get("price_info", {}).get("current_price") or r.get("price") or 0
        change = r.get("price_info", {}).get("price_change") or r.get("price_change") or 0
        rec = r.get("recommendation", "")
        scores = r.get("scores", {})
        ai = r.get("ai_analysis") or r.get("analysis_text") or ""
        # ai may be markdown; keep as-is - it's likely safe because content is generated internally
        part = f"""
        <div style="border:1px solid #eee;border-radius:6px;padding:12px;margin-bottom:12px;background:#fff">
          <h3 style="margin:0 0 8px 0">{stock_name} ({code})</h3>
          <p style="margin:0 0 4px 0"><strong>当前价格：</strong> {price} &nbsp; <strong>涨跌幅：</strong> {change}%</p>
          <p style="margin:0 0 4px 0"><strong>建议：</strong> {rec}</p>
          <p style="margin:0 0 6px 0"><strong>评分：</strong> 综合 {scores.get('comprehensive', '--')}, 技术 {scores.get('technical','--')}, 基本面 {scores.get('fundamental','--')}</p>
          <div style="background:#fafafa;padding:10px;border-radius:6px;color:#333">{ai}</div>
        </div>
        """
        parts.append(part)
    footer = "<p>此邮件由 Portfolio Guardian 自动发送，以帮助您跟踪自选股的最新分析结果。</p>"
    html = header + "".join(parts) + footer
    return html

# -------------------- Email sending --------------------
def create_simple_html(title: str, content: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title></head>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f4f4f4;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#f4f4f4;">
    <tr><td style="padding:20px 0;">
      <table width="700" align="center" cellpadding="0" cellspacing="0" role="presentation" style="background:#fff;border-radius:8px;">
        <tr><td style="padding:20px;border-bottom:2px solid #667eea;"><h1 style="margin:0;font-size:20px;color:#333;">{title}</h1></td></tr>
        <tr><td style="padding:20px;">{content}</td></tr>
        <tr><td style="padding:12px;border-top:1px solid #eee;text-align:center;color:#999;font-size:12px;">此邮件由 Portfolio Guardian 自动发送，请勿直接回复</td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

def send_email(to_email: str, subject: str, html_content: str) -> bool:
    """通过 SMTP 发送邮件（使用硬编码的 RESEND_API_KEY 或环境变量）"""
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
            server.login(SMTP_USER, RESEND_API_KEY)
            server.send_message(msg)
        logger.info(f"邮件发送成功到 {to_email}")
        return True
    except Exception as e:
        logger.error(f"发送邮件失败: {e}")
        return False

# -------------------- Main orchestration: send_report --------------------
def send_report(report_type: str):
    logger.info("=" * 60)
    report_names = {"morning_brief": "早市简报", "midday_review": "中市回顾", "eod_summary": "尾市总结"}
    title_prefix = report_names.get(report_type, "股市报告")
    logger.info(f"开始执行：{title_prefix}")

    # 1) 查询开启邮件的用户
    users = get_users_with_email_enabled(report_type)
    if not users:
        logger.warning("没有启用的用户，任务结束")
        return
    logger.info(f"找到 {len(users)} 个启用���用户")

    # 2) Ensure analyzer and (optionally) flask server running
    try:
        analyzer = ensure_analyzer_and_server(start_flask=True)
    except Exception as e:
        logger.error(f"无法初始化分析器/服务器: {e}")
        analyzer = None

    # 3) For each user, get watchlist, run analysis, send mail
    success_count = 0
    failed_count = 0
    for user in users:
        email = user.get("email") or user.get("contact") or ""
        user_id = user.get("resolved_user_id", "")
        logger.info(f"\n处理用户: email={email}, user_id={user_id}")

        if not email:
            logger.warning("用户没有设置邮箱，跳过")
            failed_count += 1
            continue

        watchlist = get_user_watchlist(user_id)
        logger.info(f"   找到 {len(watchlist)} 条自选股（可能仅有 name）")

        # 4) Run analysis for this user's watchlist
        reports = []
        if analyzer:
            try:
                reports = analyze_watchlist_with_analyzer(analyzer, watchlist, per_stock_timeout=PER_STOCK_TIMEOUT)
            except Exception as e:
                logger.error(f"对用户 {email} 的自选股进行分析时出错: {e}")
                reports = []
        else:
            logger.warning("分析器不可用，跳过分析步骤")

        # 5) Build email HTML and send
        html_body = build_email_html_from_reports(reports, user, title_prefix)
        full_html = create_simple_html(f"{title_prefix}", html_body)
        today = datetime.now().strftime("%Y年%m月%d日 %A")
        subject = f"{title_prefix} - {today}"

        if send_email(email, subject, full_html):
            success_count += 1
        else:
            failed_count += 1

    logger.info("\n" + "=" * 60)
    logger.info(f"任务完成: 成功 {success_count}, 失败 {failed_count}")
    logger.info("=" * 60)


# -------------------- CLI --------------------
def main():
    if len(sys.argv) < 2:
        print("用法: python email_system.py <report_type>")
        print("")
        print("报告类型:")
        print("  morning_brief  - 早市简报 (09:00)")
        print("  midday_review  - 中市回顾 (12:00)")
        print("  eod_summary    - 尾市总结 (16:30)")
        print("")
        print("示例:")
        print("  python email_system.py morning_brief")
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
