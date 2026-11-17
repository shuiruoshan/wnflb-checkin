#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# 文件生成 / 最后修改时间：2026-11-17 17:50:17
# 版本：v5（登录成功必须以"多端点确认登录态 + auth Cookie 已下发"为准）
# 说明：时间戳由 WorkBuddy 在本文件被修改时写入，便于对照 Actions 运行的是哪一版。
# ============================================================================
"""
福利吧论坛 (wnflb2023.com) 自动签到脚本
=======================================
基于 Discuz! X3.4，支持两种认证方式：
  1. 账号密码登录（自动处理验证码，ddddocr 识别）
  2. 直接传入 Cookie（兼容旧方式）

特性：
  - 登录成功后把 Cookie 保存到本地/cache，下次优先复用
  - Cookie 失效自动重新用账号密码登录（含验证码）
  - 新 IP 登录需要验证码时自动拉取并识别（ddddocr）
  - 支持 PushPlus / Server 酱 微信推送
  - 论坛为 GBK 编码，已做兼容

环境变量：
  FORUM_USERNAME   账号
  FORUM_PASSWORD   密码
  FORUM_COOKIE     直接传入的 Cookie 字符串（优先级高于 cookie 文件）
  COOKIE_FILE      Cookie 缓存文件路径（默认 cookies.json）
  PUSHPLUS_TOKEN   PushPlus 推送 token
  SERVERCHAN_KEY   Server 酱推送 key
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests

# ========================= 配置 =========================
BASE_URL = "https://www.wnflb2023.com"
FORUM_URL = BASE_URL + "/forum.php"
LOGIN_PAGE_URL = BASE_URL + "/member.php?mod=logging&action=login"
TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ========================= 工具函数 =========================

def get_cst_time():
    utc_now = datetime.now(timezone.utc)
    return (utc_now + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def parse_cookies(raw):
    """Cookie 字符串 -> 字典"""
    cookies = {}
    for item in raw.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def get_page_text(resp):
    """优先按 GBK 解码（论坛是 GBK）"""
    if resp.encoding and resp.encoding.lower() in ("gbk", "gb2312", "gb18030"):
        return resp.text
    try:
        return resp.content.decode("gbk")
    except (UnicodeDecodeError, LookupError):
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text


def fetch_forum(session):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(FORUM_URL, timeout=TIMEOUT)
            return resp
        except requests.RequestException as e:
            print(f"  [网络] 第 {attempt}/{MAX_RETRIES} 次请求失败: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return None


# ========================= Cookie 读写 =========================

def _site_host():
    return urllib.parse.urlsplit(BASE_URL).hostname or "www.wnflb2023.com"


def apply_cookie_dict(session, data):
    """把 name->value 字典写入 session，**绑定到站点域名**。

    为什么不能直接 session.cookies.update(dict)：那样写入的 Cookie domain 为空，
    会与登录后服务端下发的同名 Cookie（domain=站点）**并存**，请求时两个 auth
    同时发出，PHP 取到哪一个不确定——旧的那个会让服务端认为"未登录"。
    这正是「返回登录成功页、紧接着首页却是游客」的典型成因。
    """
    host = _site_host()
    for k, v in (data or {}).items():
        if not k:
            continue
        session.cookies.set(k, v, domain=host, path="/")


def load_cookies(session, raw_cookie, cookie_file):
    """把 cookie 载入 session。返回是否成功载入。"""
    if raw_cookie:
        apply_cookie_dict(session, parse_cookies(raw_cookie))
        return True
    if cookie_file and os.path.exists(cookie_file):
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                apply_cookie_dict(session, data)
                return True
        except Exception as e:
            print(f"  [Cookie] 读取缓存失败: {e}")
    return False


def save_cookies(session, cookie_file):
    if not cookie_file:
        return
    try:
        data = {c.name: c.value for c in session.cookies}
        with open(cookie_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  [Cookie] 已保存到 {cookie_file}")
    except Exception as e:
        print(f"  [Cookie] 保存失败: {e}")


# 不可缓存、且「必须登录才能访问」的探针端点。
# 为什么不能只看首页：forum.php 常被 CDN / 缓存插件做成「游客缓存」，
# 已登录也可能拿到游客页（discuz_uid=0），只查首页会把成功登录误判成失败。
LOGIN_PROBES = (
    ("首页", FORUM_URL),
    ("个人中心", BASE_URL + "/home.php?mod=space&do=profile"),
    ("设置页", BASE_URL + "/home.php?mod=spacecp"),
)


def auth_cookie(session):
    """返回 (name, value)：Discuz 认证 Cookie（形如 <cookiepre>auth）。
    排除 invite_auth —— 那是邀请链接的 Cookie，不是登录凭据。"""
    for c in session.cookies:
        n = c.name or ""
        if n.endswith("auth") and "invite" not in n:
            return n, c.value
    return None, None


def drop_stale_auth(session):
    """登录前清除可能残留的旧 auth Cookie。
    失效的 auth 会让服务端进入「疑似已登录」的异常分支，干扰重新登录。"""
    dropped = []
    for c in list(session.cookies):
        n = c.name or ""
        if n.endswith("auth") and "invite" not in n:
            try:
                session.cookies.clear(c.domain, c.path, c.name)
                dropped.append(n)
            except Exception:
                pass
    if dropped:
        print(f"  [登录] 已清除失效的认证 Cookie: {', '.join(dropped)}")


def probe_login_state(session, verbose=True):
    """多端点探测登录态：任一端点显示已登录即判定已登录。

    返回 (logged, html, detail)。html 是首页内容（供后续取 formhash 用）。
    """
    details = []
    forum_html = ""
    for name, url in LOGIN_PROBES:
        try:
            r = session.get(url, timeout=TIMEOUT)
            html = get_page_text(r)
            if name == "首页":
                forum_html = html
            uid = re.search(r"discuz_uid\s*=\s*'(\d+)'", html)
            ok = check_logged_in(html)
            uid_txt = uid.group(1) if uid else "未出现"
            details.append(f"{name}={'已登录' if ok else '游客(uid=' + uid_txt + ')'}")
            if verbose:
                print(f"  [登录态] {name} -> {'已登录' if ok else '游客'}"
                      f"（HTTP {r.status_code}, uid={uid_txt}, 长度 {len(html)}）")
            if ok:
                return True, forum_html or html, "; ".join(details)
        except requests.RequestException as e:
            details.append(f"{name}=请求失败")
            if verbose:
                print(f"  [登录态] {name} -> 请求失败: {e}")
    return False, forum_html, "; ".join(details)


def dump_login_diag(session):
    """登录态校验失败时输出诊断信息：认证 Cookie 是否下发、各探针端点结果。"""
    auths = [(c.name, c.domain, c.path, len(c.value or "")) for c in session.cookies
             if (c.name or "").endswith("auth") and "invite" not in (c.name or "")]
    print("  [诊断] ======== 登录态校验失败，开始诊断 ========")
    if auths:
        for name, dom, path, n in auths:
            print(f"  [诊断] 认证 Cookie: {name} domain={dom} path={path} 值长度={n}")
        if len(auths) > 1:
            print("  [诊断] 警告: 存在多个 auth Cookie（域名不同会导致服务端取错凭据）")
    else:
        print("  [诊断] 认证 Cookie: 未下发")
        print("  [诊断] 说明: 服务端没给出登录凭据，即登录并未真正成功（成功页可能是异常页）")
    print(f"  [诊断] 当前 Cookie: {', '.join(sorted({c.name for c in session.cookies}))}")
    probe_login_state(session, verbose=True)
    print("  [诊断] ========================================")


def verify_login(session):
    """确认登录态（多端点）。返回 (bool, html)。"""
    logged, html, _detail = probe_login_state(session, verbose=False)
    if not html:
        html = ""
    return logged, html


def check_logged_in(html):
    """
    检测页面是否处于登录态。
    最可靠信号：页面 JS 里的 discuz_uid（游客为 '0'，登录后为真实 UID）。
    用它能避免把访客页里的 fx_checkin 模块误判成已登录。
    """
    m = re.search(r"discuz_uid\s*=\s*'(\d+)'", html)
    if m:
        return m.group(1) != "0"
    # 兜底：有登出链接视为已登录
    if 'class="logout"' in html or "mod=logging&action=logout" in html:
        return True
    # 兜底：出现登录表单视为未登录
    if 'name="username"' in html and 'name="password"' in html:
        return False
    return False


def _looks_like_js(t):
    """判断抓到的"文案"是否其实是内联 JS（Discuz 成功/跳转页常见），
    这类文本不是给人看的错误消息，必须剔除，否则会被当成错误原因输出。"""
    if not t:
        return True
    js_marks = ("$(", "function(", "window.location", "setTimeout", "document.write", "innerHTML", "getElementById", ".style.display")
    return any(k in t for k in js_marks)


def extract_message(html):
    """提取 Discuz 提示信息页的正文（登录失败/成功提示、签到提示等）。"""
    # 方式1: <div id="messagetext"> 内的 <p> 文案（Discuz 提示页标准结构）
    m = re.search(r'id="messagetext"[^>]*>(.*?)</div>', html, re.DOTALL)
    if m:
        block = m.group(1)
        # 先剥掉内联脚本/样式，否则会把 JS 当文案（旧版就是这么把成功页判成失败的）
        block = re.sub(r"<script.*?</script>", " ", block, flags=re.DOTALL | re.I)
        block = re.sub(r"<style.*?</style>", " ", block, flags=re.DOTALL | re.I)
        p = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
        t = re.sub(r"<[^>]+>", " ", p.group(1) if p else block)
        t = re.sub(r"\s+", " ", t).strip()
        if t and not _looks_like_js(t):
            return t
    # 方式2: 通用 alert 文案 div
    m = re.search(r'class="alert_(?:right|error|info)"[^>]*>(.*?)</div>', html, re.DOTALL)
    if m:
        t = re.sub(r"<script.*?</script>", " ", m.group(1), flags=re.DOTALL | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t and not _looks_like_js(t):
            return t
    return ""


# ========================= 验证码形近字符纠正 =========================

# ddddocr 最容易混淆的字符对。check 接口经实测大小写不敏感，
# 故这里只处理"数字/字母形近"，不做大小写变体。
CONFUSABLES = {
    "0": ["O", "D"], "O": ["0", "D"], "D": ["0", "O"],
    "1": ["l", "I", "7"], "l": ["1", "I"], "I": ["1", "l"], "7": ["1"],
    "5": ["S"], "S": ["5"],
    "8": ["B"], "B": ["8"],
    "2": ["Z"], "Z": ["2"],
    "6": ["G"], "G": ["6"],
    "9": ["g", "q"], "g": ["9", "q"], "q": ["9", "g"],
    "3": ["E"], "E": ["3"],
    "4": ["A"], "A": ["4"],
}


def generate_captcha_candidates(code, max_total=40):
    """
    根据 ddddocr 的识别结果生成形近候选，按"改动字符数"升序排列：
      1) 原样
      2) 单个字符替换（最常见：ddddocr 只错 1 位）
      3) 两个字符同时替换（限量，避免组合爆炸）
    """
    code = (code or "").strip()
    if not code:
        return []
    out, seen = [], set()

    def add(c):
        if c and c not in seen:
            seen.add(c)
            out.append(c)

    add(code)
    # 单字符替换
    for i, ch in enumerate(code):
        for alt in CONFUSABLES.get(ch, []):
            add(code[:i] + alt + code[i + 1:])
    # 双字符替换（限量）
    if len(out) < max_total:
        pos = [i for i, ch in enumerate(code) if ch in CONFUSABLES]
        for a in range(len(pos)):
            for b in range(a + 1, len(pos)):
                i, j = pos[a], pos[b]
                for ai in CONFUSABLES[code[i]]:
                    for bj in CONFUSABLES[code[j]]:
                        chars = list(code)
                        chars[i], chars[j] = ai, bj
                        add("".join(chars))
                        if len(out) >= max_total:
                            return out
    return out[:max_total]


# ========================= 登录（账号密码） =========================

def fetch_login_page(session):
    """GET 登录页，返回 (html, formhash, loginhash)"""
    resp = session.get(LOGIN_PAGE_URL, timeout=TIMEOUT)
    html = get_page_text(resp)
    formhash_m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', html)
    formhash = formhash_m.group(1) if formhash_m else None
    # loginhash 在表单 action 的 URL 里
    lh_m = re.search(r"loginhash=([A-Za-z0-9]+)", html)
    loginhash = lh_m.group(1) if lh_m else None
    return html, formhash, loginhash


def detect_captcha(html):
    """
    识别登录页/挑战页是否需要验证码，并提取关键参数。
    支持两种形态：
      A. 标准：misc.php?mod=seccode&update=<rand>&idhash=<hash>
      B. 二次挑战（新 IP）：响应带 name="auth" 令牌
         + updateseccode('IDHASH', ...) / <span id="seccode_IDHASH">
    返回 dict: {needed, idhash, update, seccodehash, auth}
    """
    res = {
        "needed": False,
        "idhash": "",
        "update": str(int(time.time() * 1000)),
        "seccodehash": "",
        "auth": "",
    }

    # auth 令牌（二次挑战，必须随登录一起提交）
    am = re.search(r'name="auth"\s+value="([A-Za-z0-9%_./=+]+)"', html)
    if am:
        res["auth"] = am.group(1)

    # idhash：优先 updateseccode('IDHASH', ...) 或 <span id="seccode_IDHASH">
    ih = re.search(r"updateseccode\(\s*['\"]([A-Za-z0-9]+)['\"]", html)
    if not ih:
        ih = re.search(r'id="seccode_([A-Za-z0-9]+)"', html)
    if not ih:
        sm = re.search(
            r"misc\.php\?mod=seccode&update=([^&\"']+)&idhash=([A-Za-z0-9]+)", html
        )
        if sm:
            res["idhash"] = sm.group(2)
            res["update"] = sm.group(1)
    if ih and not res["idhash"]:
        res["idhash"] = ih.group(1)

    # 输入框 id 形如 seccodeverify_<idhash>
    if not res["idhash"]:
        sid = re.search(r'id="seccodeverify_([A-Za-z0-9]+)"', html)
        if sid:
            res["idhash"] = sid.group(1)
    # 隐藏域 seccodehash
    if not res["idhash"]:
        sh = re.search(r'name="seccodehash"\s+value="([A-Za-z0-9]+)"', html)
        if sh:
            res["idhash"] = sh.group(1)

    if not res["idhash"] and re.search(r'name="seccodeverify"', html):
        res["idhash"] = "SkyV"  # 极端兜底

    if res["idhash"] or res["auth"]:
        res["needed"] = True

    if res["idhash"]:
        sh = re.search(r'name="seccodehash"\s+value="([A-Za-z0-9]+)"', html)
        res["seccodehash"] = sh.group(1) if sh else res["idhash"]

    return res


def extract_login_fields(html):
    """从登录/挑战页提取 formhash、loginhash、auth（用于二次提交）。"""
    fh = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', html)
    lh = re.search(r"loginhash=([A-Za-z0-9]+)", html)
    auth = re.search(r'name="auth"\s+value="([A-Za-z0-9%_]+)"', html)
    return (
        fh.group(1) if fh else None,
        lh.group(1) if lh else None,
        auth.group(1) if auth else None,
    )


def solve_captcha(session, cap, use_ocr_cache=False, max_tries=10):
    """
    拉取验证码图片并用 ddddocr 识别，清洗为"恰好 4 位纯字母数字"。
    多次换图重试，直到拿到一个干净的 4 位结果。
    返回识别出的验证码字符串，失败返回 None。

    use_ocr_cache=True 时复用单例 ddddocr（模型加载慢，避免每轮重建）；
    否则保持旧行为每次新建，兼容旧调用。
    """
    if use_ocr_cache:
        try:
            ocr = get_ocr()
        except ImportError:
            print("  [验证码] 未安装 ddddocr，请先 pip install ddddocr")
            return None
    else:
        try:
            import ddddocr  # 仅在需要验证码时才 import
        except ImportError:
            print("  [验证码] 未安装 ddddocr，请先 pip install ddddocr")
            return None
        ocr = ddddocr.DdddOcr(show_ad=False)

    # 论坛/WAF 对 seccode 图片接口要求同源 Referer，否则返回 Access Denied
    headers = {"Referer": LOGIN_PAGE_URL}
    for attempt in range(1, max_tries + 1):
        try:
            update = str(int(time.time() * 1000))
            img_url = (
                f"{BASE_URL}/misc.php?mod=seccode"
                f"&update={update}&idhash={cap['idhash']}"
            )
            r = session.get(img_url, timeout=TIMEOUT, headers=headers)
            # 校验确实是图片（PNG/JPEG 魔数），避免把 Access Denied 当图片识别
            if r.status_code != 200 or len(r.content) < 100:
                print(f"  [验证码] 第 {attempt} 次拉取图片失败(status={r.status_code}, len={len(r.content)})")
                continue
            if r.content[:4] not in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1"):
                print(f"  [验证码] 第 {attempt} 次返回非图片(可能被拦截), len={len(r.content)}")
                continue
            raw = (ocr.classification(r.content) or "").strip()
            code = sanitize_code(raw)
            if code:
                return code
            # 脏输出（汉字/符号/位数不对）直接换图重识
            print(f"  [验证码] 识别结果不合法([{raw}])，换新图 ...")
        except Exception as e:
            print(f"  [验证码] 第 {attempt} 次识别异常: {e}")
    return None


def verify_captcha_code(session, cap, code):
    """
    调用 Discuz 验证码校验接口（action=check）。仅作"形近纠正"的辅助提示，
    不作为"是否提交登录"的判据——实测发现：本接口返回 succeed 与最终登录提交
    是否认可该验证码并不总是完全一致（尤其风控 IP 下），若把它当提交前置条件，
    会出现"check 通过但提交一直报验证码错误"的死循环。

    真实校验（已实测）：成功返回 <root><![CDATA[succeed]]></root>；
    错误返回 <root><![CDATA[invalid]]></root>。这里用精确匹配，避免误判。
    注意：本接口不消费验证码答案，同一张图可重复 check。
    """
    url = (
        f"{BASE_URL}/misc.php?mod=seccode&action=check&inajax=1"
        f"&modid=member::logging&idhash={cap['idhash']}&secverify={code}"
    )
    try:
        r = session.get(
            url,
            timeout=TIMEOUT,
            headers={
                "Referer": LOGIN_PAGE_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        txt = get_page_text(r)
        # 精确匹配 succeed，避免把含 "succeed" 的错误页/红包页误判为成功
        m = re.search(r"<!\[CDATA\[\s*succeed\s*\]\]>", txt)
        if m:
            return True
        # 明确 invalid 或其他 CDATA（如 "1"）都视为未通过
        return False
    except Exception as e:
        print(f"  [验证码] 校验接口异常: {e}")
        return False


_OCR_INSTANCE = None


def get_ocr():
    """复用单个 ddddocr 实例（模型加载较慢，避免每次识别都重建）。"""
    global _OCR_INSTANCE
    if _OCR_INSTANCE is None:
        import ddddocr
        try:
            import importlib.metadata as _md
            _ver = _md.version("ddddocr")
        except Exception:
            _ver = getattr(ddddocr, "__version__", "unknown")
        print(f"  [验证码] ddddocr 版本={_ver}（推荐 1.6.1；版本差异会影响识别准确率）")
        _OCR_INSTANCE = ddddocr.DdddOcr(show_ad=False)
    return _OCR_INSTANCE


def sanitize_code(raw, expect_len=4):
    """
    清洗 ddddocr 输出：只保留字母数字，去掉混入的汉字/空格/符号。
    返回恰好 expect_len 位的纯字母数字串；不符合返回 ''。
    Actions 上 ddddocr 出现过 'CCJ儿'、'cQ/w'、'Ot id' 等脏输出，
    直接送进 check 全是无效请求，必须先清洗。
    """
    if not raw:
        return ""
    cleaned = "".join(ch for ch in str(raw) if ch.isascii() and ch.isalnum())
    return cleaned if len(cleaned) == expect_len else ""


def solve_captcha_confirmed(session, cap, max_images=8):
    """
    加固版识别：拉图 -> ddddocr 识别 -> 清洗 -> 生成形近候选 ->
    用 action=check 作为"预言机"逐个验证，返回服务端确认正确的 code。

    原理（已实测）：
      - action=check 返回 succeed 即代表该 code 与服务端答案一致；
      - 同一张图可以 repeat check（实测不消费验证码），因此可对形近候选逐个试；
      - check 成功还会写入 seccode<idhash> cookie，正是登录提交所需凭据。
    针对 Actions 上 ddddocr 输出汉字/符号的问题，识别结果先经 sanitize_code
    清洗为"恰好 4 位纯字母数字"，脏输出直接换图重识（最多 max_images 张）。
    返回 (code, True)；所有候选都失败返回 (None, False)。
    """
    try:
        ocr = get_ocr()
    except ImportError:
        print("  [验证码] 未安装 ddddocr，请先 pip install ddddocr")
        return None, False

    headers = {"Referer": LOGIN_PAGE_URL}
    for img_try in range(1, max_images + 1):
        try:
            update = str(int(time.time() * 1000))
            img_url = (
                f"{BASE_URL}/misc.php?mod=seccode"
                f"&update={update}&idhash={cap['idhash']}"
            )
            r = session.get(img_url, timeout=TIMEOUT, headers=headers)
            if r.status_code != 200 or len(r.content) < 100:
                print(f"  [验证码] 第 {img_try} 张图拉取失败(status={r.status_code}, len={len(r.content)})")
                continue
            if r.content[:4] not in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1"):
                print(f"  [验证码] 第 {img_try} 张图返回非图片(可能被拦截)")
                continue
            raw = (ocr.classification(r.content) or "").strip()

            # 关键：清洗脏输出（汉字/空格/符号），只接受 4 位纯字母数字
            code = sanitize_code(raw)
            if not code:
                print(f"  [验证码] 第 {img_try} 张识别结果不合法([{raw}])，换新图 ...")
                continue

            # 候选：原样 + 形近替换；check 大小写不敏感，故大小写不额外展开
            cands = generate_captcha_candidates(code)
            print(f"  [验证码] 识别=[{raw}]->[{code}]，校验接口确认({len(cands)} 个候选) ...")
            for cand in cands:
                if verify_captcha_code(session, cap, cand):
                    if cand != code:
                        print(f"  [验证码] 形近纠正: {code} -> {cand}")
                    return cand, True
            print(f"  [验证码] 第 {img_try} 张图的候选均未通过校验，换新图 ...")
        except Exception as e:
            print(f"  [验证码] 第 {img_try} 次处理异常: {e}")
    return None, False


def refresh_challenge(session, auth, last_html=None):
    """
    重新拉取二次验证码挑战页，取得新的 auth 与验证码参数。
    auth 令牌在提交失败后可能已失效，必须刷新，否则重试必然再次失败。
    优先从最近一次响应 HTML 中解析（Discuz 失败响应常直接带新挑战页），
    否则带旧 auth GET 挑战页。
    返回 (formhash, loginhash, new_auth, cap) 或 None。
    """
    chtml = last_html or ""
    c_fh, c_lh, c_auth = extract_login_fields(chtml)
    cap = detect_captcha(chtml)
    new_auth = urllib.parse.unquote(c_auth) if c_auth else None
    if not (new_auth and c_fh and c_lh and cap["needed"] and cap["idhash"]):
        try:
            r = session.get(
                f"{BASE_URL}/member.php",
                params={"mod": "logging", "action": "login", "auth": auth},
                timeout=TIMEOUT, headers={"Referer": LOGIN_PAGE_URL},
            )
            chtml = get_page_text(r)
            c_fh, c_lh, c_auth = extract_login_fields(chtml)
            new_auth = urllib.parse.unquote(c_auth) if c_auth else auth
            cap = detect_captcha(chtml)
        except requests.RequestException:
            return None
    if not (c_fh and c_lh and cap["needed"] and cap["idhash"]):
        return None
    return c_fh, c_lh, new_auth, cap


def _submit_login(session, formhash, loginhash, username="", password="",
                  seccode="", auth="", seccodehash="", challenge=False,
                  inajax=False):
    """
    执行一次登录 POST。返回 (ok, msg, resp_html)。

    challenge=True 时按 Discuz「二次验证码挑战」提交：
      - 凭据已由第一次提交的 auth 令牌在服务端关联，无需重复发送账号密码
      - 字段集对齐浏览器真实成功包：formhash/referer/auth/questionid/answer/
        seccodehash/seccodemodid=member::logging/seccodeverify
      - 登录 URL 带 &inajax=1
      - auth 调用方需先用 urllib.parse.unquote 还原，再由本函数统一按
        form-urlencoded 编码（与浏览器/真实 curl 字节级一致）
    """
    if challenge:
        data = {
            "formhash": formhash,
            "referer": BASE_URL + "/",
            "auth": auth,
            "questionid": "0",
            "answer": "",
            "seccodehash": seccodehash or "",
            "seccodemodid": "member::logging",
            "seccodeverify": seccode,
        }
    else:
        data = {
            "formhash": formhash,
            "referer": BASE_URL + "/",
            "loginfield": "username",
            "username": username,
            "password": password,
            "questionid": "0",
            "answer": "",
            "cookietime": "2592000",
        }
        if seccode:
            data["seccodeverify"] = seccode
            if seccodehash:
                data["seccodehash"] = seccodehash

    login_url = (
        f"{BASE_URL}/member.php?mod=logging&action=login"
        f"&loginsubmit=yes&loginhash={loginhash}"
    )
    if challenge or inajax:
        login_url += "&inajax=1"
    try:
        r = session.post(
            login_url,
            data=data,
            timeout=TIMEOUT,
            allow_redirects=True,
            headers={"Referer": LOGIN_PAGE_URL},
        )
    except requests.RequestException as e:
        return False, f"登录请求异常: {e}", ""

    txt = get_page_text(r)

    # ---- 最高优先级：识别 Discuz「登录成功页」 ----
    # 此前版本把登录成功页误判为失败：服务端登录成功后返回的是带 JS 的跳转页，
    # 形如：
    #   if($('succeedmessage')) {$('succeedmessage').innerHTML = '';}
    #   $('main_message').style.display = 'none';
    #   $('main_success').style.display = '';
    #   $('succeedlocation').innerHTML = '...';      // 含欢迎语/用户名
    #   setTimeout("window.location.href='https://.../?'", 3000);
    # 判据必须用「JS 调用形式」，不能只查 id 名——因为登录页模板里本来就预置了
    # 空的 <p id="succeedmessage"></p>/<p id="succeedlocation"></p> 占位元素，
    # 只查 id 名会把登录页/失败页误判成功（已实测）。故匹配带 $('...') 的调用。
    _success_markers = (
        "$('succeedmessage').innerHTML",
        "$('main_success').style.display",
        "setTimeout(\"window.location.href=",
    )
    if any(mk in txt for mk in _success_markers):
        # 关键：页面说"成功" ≠ 真的建立了登录态。
        # 线上出现过「服务端返回登录成功页，但认证 Cookie 未生效、首页仍是 uid=0」，
        # 旧逻辑在这里直接 return True，才导致后面报「登录后首页校验未通过」。
        # 所以必须以探针端点（多端点）确认为准。
        logged, _html, detail = probe_login_state(session, verbose=False)
        if logged:
            print(f"  [登录] 已确认登录态: {detail}")
            return True, "登录成功", txt
        print(f"  [登录] 注意: 返回了登录成功页，但会话未建立（{detail}）")
        return False, "服务端返回了登录成功页，但会话未建立（未下发有效认证 Cookie）", txt

    msg = extract_message(txt)
    if not msg:
        # inajax=1 时消息在 CDATA 里；剥掉内联脚本再取文案
        cdata = re.search(r"<!\[CDATA\[(.*?)\]\]>", txt, re.DOTALL)
        if cdata:
            _blk = re.sub(r"<script.*?</script>", " ", cdata.group(1), flags=re.DOTALL | re.I)
            msg = re.sub(r"<[^>]+>", " ", _blk)
            msg = re.sub(r"\s+", " ", msg).strip()

    # ajax(inajax=1) 提交成功信号：Discuz 返回 succeedhandle_login(...)
    # 浏览器登录表单是 onsubmit="ajaxpost(...)"，ajaxpost 会自动追加 inajax=1，
    # 这才是真实浏览器提交路径；成功信号明确，不会被提示页 JS 干扰。
    if "succeedhandle_login" in txt:
        logged, _html, detail = probe_login_state(session, verbose=False)
        if logged:
            print(f"  [登录] 已确认登录态: {detail}")
            return True, "登录成功", txt
        print(f"  [登录] 注意: ajax 返回成功回调，但会话未建立（{detail}）")
        return False, "服务端返回成功回调，但会话未建立（未下发有效认证 Cookie）", txt

    # 仍被要求验证码（验证码错误等）
    if "请输入验证码" in txt and "auth=" in txt:
        return False, (msg or "验证码不正确，请重试"), txt

    # 权威校验：多端点确认登录态
    logged, _ = verify_login(session)
    if logged:
        # 成功页提示区含内联 JS，extract_message 会抽到脏文本，统一用干净文案
        return True, "登录成功", txt
    # 账号/密码/频率限制等明确的失败文案（避免重复加"登录失败:"前缀）
    if msg and ("密码" in msg or "用户名" in msg or "尝试" in msg):
        return False, (msg if msg.startswith("登录失败") else f"登录失败: {msg}"), txt
    return False, (msg or "登录失败（未进入登录态）"), txt


def do_login(session, username, password):
    """
    账号密码登录（含新 IP 二次验证码挑战）。
    成功返回 (True, 消息)，失败返回 (False, 消息)。
    """
    print("  [登录] 获取登录页 ...")
    html, formhash, loginhash = fetch_login_page(session)
    if not formhash or not loginhash:
        return False, "无法解析登录页(formhash/loginhash 缺失)"

    # 路径 A（当前论坛的主要情况）：登录页本身就直接要求验证码。
    # 风控 IP 下，原脚本一上来就"无验证码提交"会被直接回"验证码填写错误"。
    #
    # 核心策略（第二代）——以"登录提交结果"为唯一权威，check 仅作辅助：
    #   上一代把 check(action=check) 当"提交前置预言机"，实测在风控 IP 下会出现
    #   "check 说 succeed、但提交一直报验证码错误"的死循环（check 的 succeed 与
    #   服务端登录提交实际认可的结果并不总一致）。因此改为：OCR 识别后直接提交，
    #   用提交返回的 msg 判断是否真的过了验证码关。
    #   另外每轮都重新拉登录页（拿全新 idhash + 全新 seccode cookie），避免复用
    #   旧验证码状态导致重复失败。
    page_cap = detect_captcha(html)
    if page_cap["needed"] and page_cap["idhash"]:
        print("  [登录] 登录页要求验证码，识别后直接提交验证 ...")
        # 清掉可能残留的旧 auth Cookie：失效的 auth 会让服务端走「疑似已登录」的
        # 异常分支（返回成功页但不下发新凭据），是本次问题的可疑诱因之一。
        drop_stale_auth(session)
        last_msg = "登录页验证码重试次数已用尽"
        for attempt in range(1, 7):
            print(f"  [登录] 第 {attempt}/6 轮，验证码 idhash={page_cap['idhash']} ...")
            # OCR 识别（清洗为 4 位字母数字；脏输出自动换图）
            code = solve_captcha(session, page_cap, use_ocr_cache=True)
            if not code:
                print("  [登录] 验证码识别失败，重新拉登录页 ...")
                html, formhash, loginhash = fetch_login_page(session)
                page_cap = detect_captcha(html)
                continue
            # 两条提交路径交替尝试，互为主备：
            #   奇数轮 -> ajax(inajax=1)，这是浏览器表单 ajaxpost 的真实路径，
            #             成功时返回 succeedhandle_login 回调，信号最明确；
            #   偶数轮 -> 整页提交，返回 Discuz 提示页。
            # 某条路径在风控下被特殊处理时，另一条仍有机会拿到 auth Cookie。
            use_ajax = (attempt % 2 == 1)
            print(f"  [验证码] 识别=[{code}]，直接提交登录验证"
                  f"（{'ajax' if use_ajax else '整页'}提交）...")
            ok, msg, _ = _submit_login(
                session, formhash, loginhash, username, password,
                code, "", page_cap["seccodehash"], inajax=use_ajax,
            )
            if ok:
                return True, "登录成功(登录页验证码)"
            last_msg = msg
            # 页面声称成功但没建立会话 -> 重拉登录页换另一种提交路径
            if "成功页" in msg or "成功回调" in msg:
                print(f"  [登录] 第 {attempt} 轮：{msg}，重拉登录页换提交方式 ...")
                html, formhash, loginhash = fetch_login_page(session)
                page_cap = detect_captcha(html)
                if not (formhash and loginhash and page_cap["idhash"]):
                    return False, "重新获取登录页失败"
                continue
            # 验证码错误 -> 换全新登录页(idhash+cookie 都刷新)重试
            if "验证码" in msg and ("不正确" in msg or "错误" in msg or "填写" in msg):
                print(f"  [登录] 第 {attempt} 轮验证码未通过，换新登录页重试 ...")
                html, formhash, loginhash = fetch_login_page(session)
                page_cap = detect_captcha(html)
                if not (formhash and loginhash and page_cap["idhash"]):
                    return False, "重新获取登录页失败"
                continue
            # 明确的账号/密码/频率问题 -> 验证码关已过，直接返回
            # 注意 msg 可能已含"登录失败:"前缀（来自 _submit_login），避免重复
            if "密码" in msg or "用户名" in msg or "尝试" in msg:
                return False, msg if msg.startswith("登录失败") else f"登录失败: {msg}"
            # 其他（formhash 失效等）重拉登录页再试
            html, formhash, loginhash = fetch_login_page(session)
            page_cap = detect_captcha(html)
        return False, last_msg

    # 路径 B：登录页未要求验证码，走原来的流程。
    # 首次尝试：无验证码直接提交（老 IP / 已信任环境通常直接成功）
    ok, msg, resp_html = _submit_login(
        session, formhash, loginhash, username, password, None, None
    )
    if ok:
        return True, "登录成功"

    # 被验证码挑战：从第一次提交的响应里直接解析挑战页（auth 由本次响应给出）。
    # 关键：auth 在 HTML 里可能以 %2F 形式存在（含 /），需先 unquote 还原，
    # 再交给 _submit_login 统一按 form-urlencoded 编码，否则会被二次编码导致
    # 服务端认不出令牌、报“密码空或包含非法字符”。
    chtml = resp_html or ""
    c_fh, c_lh, auth = extract_login_fields(chtml)
    auth = urllib.parse.unquote(auth) if auth else None
    if not auth:
        am = re.search(r"auth=([A-Za-z0-9%_./=+]+)", chtml)
        auth = urllib.parse.unquote(am.group(1)) if am else None
    if not auth:
        return False, msg  # 真失败（密码错等），msg 已是原因

    # 若挑战页字段没解析全，带 auth 重新拉一次，并从页面里取最新的 auth（同样 unquote）
    cap = detect_captcha(chtml)
    if not (c_fh and c_lh and cap["needed"] and cap["idhash"]):
        print("  [登录] 触发验证码挑战，重新获取挑战页 ...")
        try:
            r = session.get(
                f"{BASE_URL}/member.php",
                params={"mod": "logging", "action": "login", "auth": auth},
                timeout=TIMEOUT,
                headers={"Referer": LOGIN_PAGE_URL},
            )
            chtml = get_page_text(r)
            c_fh, c_lh, c_auth = extract_login_fields(chtml)
            auth = urllib.parse.unquote(c_auth) if c_auth else auth
            cap = detect_captcha(chtml)
        except requests.RequestException as e:
            return False, f"获取挑战页异常: {e}"

    if not (c_fh and c_lh):
        return False, "验证码挑战页未解析出 formhash/loginhash"
    if not cap["needed"] or not cap["idhash"]:
        return False, f"验证码挑战页未解析出验证码(idhash 缺失): {msg}"

    # 二次挑战提交（不带账号密码，凭据由 auth 关联）。
    # 与路径 A 一致：以"提交结果"为唯一权威，不再拿 check 当提交前置条件
    # （风控 IP 下 check 的 succeed 与登录提交认可结果可能不一致，会造成死循环）。
    # 每轮都刷新挑战页拿新 auth + 新验证码状态，最多 6 轮。
    last_msg = "验证码识别失败"
    for attempt in range(1, 7):
        print(f"  [登录] 第 {attempt}/6 轮，验证码 idhash={cap['idhash']} ...")

        # 仅做 OCR 识别（复用实例 + 清洗 + 换图），不经过 check 判断
        code = solve_captcha(session, cap, use_ocr_cache=True)
        if not code:
            last_msg = "验证码识别失败（未能得到 4 位识别结果）"
            print(f"  [登录] {last_msg}，刷新挑战页后重试 ...")
            refreshed = refresh_challenge(session, auth)
            if refreshed:
                c_fh, c_lh, auth, cap = refreshed
            continue
        print(f"  [验证码] 识别=[{code}]，直接提交验证 ...")

        ok2, last_msg, resp2 = _submit_login(
            session, c_fh, c_lh, username, password, code, auth,
            cap["seccodehash"], challenge=True,
        )
        if ok2:
            return True, "登录成功(已通过验证码)"

        # 验证码不正确 或 凭据(auth)失效：统一刷新挑战页拿新 auth 再重试
        if ("验证码" in last_msg and ("不正确" in last_msg or "错误" in last_msg or "填写" in last_msg)) \
                or "密码" in last_msg or "用户名" in last_msg:
            print(f"  [登录] 第 {attempt} 轮提交失败({last_msg})，重拉挑战页刷新 auth ...")
            refreshed = refresh_challenge(session, auth, resp2)
            if refreshed:
                c_fh, c_lh, auth, cap = refreshed
            continue
        # 其他失败不再重试
        return False, last_msg
    return False, last_msg


# ========================= 签到 =========================

def check_already_signed(html):
    m = re.search(r"fx_chk_menu\s*=\s*(true|false)", html)
    if m:
        return m.group(1) == "true"
    return False


def extract_formhash(html):
    m = re.search(r"fx_checkin:checkin&formhash=([a-f0-9]+)&([a-f0-9]+)", html)
    if m:
        return m.group(1), m.group(2)
    return None, None


def do_checkin(session, formhash, fx_formhash):
    url = (
        f"{BASE_URL}/plugin.php?id=fx_checkin:checkin"
        f"&formhash={formhash}&{fx_formhash}&inajax=1"
    )
    headers = {
        "Referer": FORUM_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT, headers=headers)
            return resp.text
        except requests.RequestException as e:
            print(f"  [签到] 第 {attempt}/{MAX_RETRIES} 次请求失败: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    return None


def parse_result(text):
    if text is None:
        return False, "网络请求失败"
    cdata = re.search(r"<!\[CDATA\[(.*?)\]\]>", text, re.DOTALL)
    content = cdata.group(1) if cdata else text
    clean = re.sub(r"<[^>]+>", " ", content).strip()
    clean = re.sub(r"\s+", " ", clean)

    if "签到成功" in clean:
        rank = re.search(r"第\s*(\d+)\s*个", clean)
        if rank:
            return True, f"签到成功！今日第 {rank.group(1)} 个签到"
        return True, "签到成功！"
    if "已经签到" in clean or "已签到" in clean:
        return True, "今日已签到（重复签到）"
    if "先登录" in clean or "请登录" in clean:
        return False, "Cookie 已过期，请重新获取"
    if "补签" in clean and "成功" in clean:
        return True, "补签成功"
    return False, f"未知响应: {clean[:200]}"


# ========================= 通知 =========================

def send_notification(title, content):
    token = os.environ.get("PUSHPLUS_TOKEN", "")
    if token:
        try:
            resp = requests.post(
                "http://www.pushplus.plus/send",
                json={"token": token, "title": title, "content": content, "template": "txt"},
                timeout=10,
            )
            print(f"  [PushPlus] {resp.json().get('msg', 'unknown')}")
        except Exception as e:
            print(f"  [PushPlus] 发送失败: {e}")

    key = os.environ.get("SERVERCHAN_KEY", "")
    if key:
        try:
            resp = requests.post(
                f"https://sctapi.ftqq.com/{key}.send",
                data={"title": title, "desp": content},
                timeout=10,
            )
            print(f"  [Server酱] {resp.json().get('message', 'unknown')}")
        except Exception as e:
            print(f"  [Server酱] 发送失败: {e}")

    if not token and not key:
        print("  (未配置推送通知)")


# ========================= 调试：解析登录页 =========================

def inspect_login():
    """仅拉取登录页并打印解析结果，用于排查（无需账号密码）。"""
    session = requests.Session()
    session.headers.update(HEADERS)
    html, formhash, loginhash = fetch_login_page(session)
    cap = detect_captcha(html)
    print("=== 登录页解析结果 ===")
    print(f"  可访问: {bool(html)}")
    print(f"  formhash : {formhash}")
    print(f"  loginhash: {loginhash}")
    print(f"  需要验证码: {cap['needed']}")
    if cap["needed"]:
        print(f"  idhash   : {cap['idhash']}")
        print(f"  seccodehash: {cap['seccodehash']}")
    print("======================")


def mark_refreshed():
    """重新登录成功并已写盘时，向 GitHub Actions 输出 refreshed=true，
    通知 workflow 删除旧缓存并保存新 Cookie（避免缓存累积 / 过期不更新）。"""
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        try:
            with open(p, "a") as f:
                f.write("refreshed=true\n")
        except OSError:
            pass


# ========================= 主流程 =========================

def main():
    parser = argparse.ArgumentParser(description="福利吧论坛自动签到")
    parser.add_argument("--username", default=os.environ.get("FORUM_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("FORUM_PASSWORD", ""))
    parser.add_argument("--cookie", default=os.environ.get("FORUM_COOKIE", ""),
                        help="直接传入 Cookie 字符串")
    parser.add_argument("--cookie-file", default=os.environ.get("COOKIE_FILE", "cookies.json"),
                        help="Cookie 缓存文件路径（默认 cookies.json）")
    parser.add_argument("--no-save", action="store_true", help="不保存 Cookie")
    parser.add_argument("--mode", default="万能福利吧签到", help="签到模式标签(万能福利吧签到/万能福利吧补签)")
    parser.add_argument("--inspect", action="store_true",
                        help="仅解析登录页并打印结果后退出")
    args = parser.parse_args()

    if args.inspect:
        inspect_login()
        return

    now = get_cst_time()
    print("=" * 50)
    print("  福利吧论坛自动签到")
    print(f"  模式: {args.mode}   时间: {now}")
    print("=" * 50)

    session = requests.Session()
    session.headers.update(HEADERS)

    html = None
    # 1) 尝试用缓存/Cookie 直接登录
    if load_cookies(session, args.cookie, args.cookie_file):
        print("[1] 已载入 Cookie，校验登录态 ...")
        logged, html = verify_login(session)
        if logged:
            print("  -> Cookie 有效，直接签到")
        else:
            print("  -> Cookie 已过期")
            html = None
    else:
        print("[1] 未找到可用 Cookie")

    # 2) Cookie 不可用 -> 账号密码登录
    if html is None:
        if not (args.username and args.password):
            msg = "Cookie 无效，且未提供账号密码(FORUM_USERNAME/FORUM_PASSWORD)"
            print(f"[FAIL] {msg}")
            send_notification("[签到失败] 需登录", f"时间:{now}\n错误:{msg}")
            sys.exit(1)
        print("[2] 使用账号密码登录 ...")
        ok, msg = do_login(session, args.username, args.password)
        if not ok:
            print(f"[FAIL] 登录失败: {msg}")
            # 「服务端说成功但会话没建立」这类问题必须留下诊断线索
            # （认证 Cookie 是否下发 / 各端点 uid），否则无从判断是风控还是账号状态。
            if "会话未建立" in msg or "成功页" in msg or "成功回调" in msg:
                dump_login_diag(session)
            send_notification("[签到失败] 登录失败", f"时间:{now}\n错误:{msg}")
            sys.exit(1)
        print(f"  -> {msg}")
        if not args.no_save:
            save_cookies(session, args.cookie_file)
            mark_refreshed()
        # 登录成功后再次确认登录态（多端点，任一显示已登录即可）。
        # 不能只查 forum.php —— 它可能被 CDN/缓存插件做成「游客缓存」，
        # 已登录也返回 uid=0，只查首页会误判为失败（线上出现过）。
        logged, html, detail = probe_login_state(session, verbose=True)
        for _try in range(2):
            if logged:
                break
            print(f"  [登录] 登录态尚未确认（{detail}），1.5s 后重试校验 ...")
            time.sleep(1.5)
            logged, html, detail = probe_login_state(session, verbose=True)
        if not logged:
            print("[FAIL] 登录后登录态校验未通过")
            dump_login_diag(session)
            print("[FAIL] 诊断提示：认证 Cookie 未下发 => 登录未真正成功（服务端仅回了成功页）；"
                  "Cookie 已下发但仍判游客 => 多为账号状态异常或站点风控。")
            sys.exit(1)

    # 3) 签到
    print("[3] 检查签到状态 ...")
    if check_already_signed(html):
        print("[OK] 今日已签到，无需重复操作")
        send_notification(f"[签到成功] {args.mode}",
                          f"时间:{now}\n状态:今日已签到")
        sys.exit(0)
    print("  -> 今日尚未签到，执行签到 ...")

    formhash, fx_formhash = extract_formhash(html)
    if not formhash:
        msg = "无法提取 formhash，页面结构可能已变化"
        print(f"[FAIL] {msg}")
        send_notification(f"[签到失败] {args.mode}", f"时间:{now}\n错误:{msg}")
        sys.exit(1)

    text = do_checkin(session, formhash, fx_formhash)
    success, message = parse_result(text)
    if success:
        print(f"[OK] {message}")
        send_notification(f"[签到成功] {args.mode}",
                          f"时间:{now}\n结果:{message}")
    else:
        print(f"[FAIL] {message}")
        send_notification(f"[签到失败] {args.mode}",
                          f"时间:{now}\n结果:{message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
