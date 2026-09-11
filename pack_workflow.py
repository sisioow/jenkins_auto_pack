#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent

_BROWSER_SESSION: Dict[str, Any] = {
    "playwright": None,
    "context": None,
    "user_data_dir": None,
    "headed": None,
}
_BROWSER_SESSION_LOCK = threading.Lock()

_BROWSER_EXEC_LOCK = threading.Lock()
_BROWSER_EXEC_QUEUE: "queue.Queue[Optional[tuple]]" = queue.Queue()
_BROWSER_EXEC_THREAD: Optional[threading.Thread] = None


@dataclass
class ParsedRequest:
    raw_text: str
    product_name: str
    source_branch: str
    job_name: str
    package_mode: str
    version_name: str
    version_code: str
    channel: str
    enable_proguard: bool
    enable_junkcode: bool
    use_a_icon: bool
    parameters: Dict[str, str]


def load_config(config_path: Path) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_first(patterns: List[str], text: str, flags: int = 0) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1).strip()
    return ""


def detect_package_mode(text: str) -> str:
    lowered = text.lower()
    if re.search(r"纯\s*b|纯b", lowered, re.IGNORECASE):
        return "pure_b"
    if re.search(
        r"a\s*面\s*带框架[^【】]*b\s*面\s*无游戏"
        r"|b\s*面\s*无游戏[^【】]*a\s*面\s*带框架"
        r"|a\s*面\s*带框架[，,]\s*b\s*面\s*无游戏",
        lowered,
        re.IGNORECASE,
    ):
        return "a_plus_b_no_game"
    if re.search(r"a\s*\+\s*b|a\+b|ab包|ab面", lowered, re.IGNORECASE):
        return "a_plus_b"
    if re.search(r"纯\s*a|纯a|a面无框架", lowered, re.IGNORECASE):
        return "pure_a"
    raise ValueError("未识别出包型。请在文案里带上“纯A / A+B / 纯B / A面带框架+B面无游戏”之一。")


def detect_enable_junkcode(text: str) -> bool:
    if re.search(r"不加垃圾|不要垃圾|无垃圾|不需要垃圾|不启用垃圾", text, re.IGNORECASE):
        return False
    return bool(
        re.search(
            r"垃圾代码|加垃圾代码|需加垃圾|需要垃圾代码|开启垃圾代码|启用垃圾代码|junkcode",
            text,
            re.IGNORECASE,
        )
    )


def detect_enable_proguard(text: str) -> bool:
    if re.search(r"不混淆|无混淆|不要混淆|不需要混淆", text, re.IGNORECASE):
        return False
    return bool(
        re.search(
            r"需加混淆|加混淆|需要混淆|开启混淆|启用混淆|用混淆"
            r"|[和、，,]\s*混淆"
            r"|需[^。；;]*混淆"
            r"|垃圾代码和混淆|垃圾代码、混淆",
            text,
            re.IGNORECASE,
        )
    )


def detect_proguard_and_junkcode(text: str) -> tuple:
    enable_proguard = detect_enable_proguard(text)
    enable_junkcode = detect_enable_junkcode(text)
    return enable_proguard, enable_junkcode


def detect_use_a_icon(text: str) -> bool:
    return bool(re.search(r"a面\s*icon|a面图标|用a面icon|使用a面icon|用a面图标", text, re.IGNORECASE))


def detect_market_channel_ui(text: str) -> str:
    if re.search(r"用\s*游戏\s*icon|使用\s*游戏\s*icon|游戏\s*icon", text, re.IGNORECASE):
        return "oppo"
    return "default"


def detect_gromore(text: str) -> bool:
    return bool(re.search(r"带\s*gromore", text, re.IGNORECASE))


def detect_init_ks_sdk_when_lock(text: str) -> str:
    if re.search(
        r"锁区\s*不\s*需要\s*初始化快手\s*sdk"
        r"|锁区[^。；;，,]*不需要\s*初始化快手"
        r"|不需要\s*初始化快手\s*sdk"
        r"|锁区\s*不需要",
        text,
        re.IGNORECASE,
    ):
        return "0"
    if re.search(
        r"锁区\s*需要\s*初始化快手\s*sdk"
        r"|锁区\s*需\s*初始化快手\s*sdk"
        r"|锁区[^。；;，,]*需要\s*初始化快手",
        text,
        re.IGNORECASE,
    ):
        return "1"
    return "0"


def detect_cocos_path(text: str) -> str:
    return extract_first(
        [
            r"打包路径\s*使用[:：]\s*([A-Za-z0-9_\-\./]+)",
            r"打包路径[:：]\s*([A-Za-z0-9_\-\./]+)",
            r"cocos[_\s-]*path[:：]\s*([A-Za-z0-9_\-\./]+)",
        ],
        text,
        re.IGNORECASE,
    )


def version_name_to_code(version_name: str) -> str:
    parts = version_name.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        raise ValueError("版本名称格式不正确，请使用如 0.0.1 或 1.0.1 的格式。")

    digits = "".join(str(int(part)) for part in parts)
    return digits.zfill(3)


def version_code_to_name(version_code: str) -> str:
    code = re.sub(r"\D", "", version_code)
    if not code:
        raise ValueError("版本号格式不正确，请使用如 001 或 101 的格式。")

    code = code.zfill(3)
    if len(code) != 3:
        raise ValueError("版本号应为 3 位数字，如 001 对应 0.0.1，101 对应 1.0.1。")

    return f"{int(code[0])}.{int(code[1])}.{int(code[2])}"


def resolve_version_pair(version_name: str = "", version_code: str = "") -> tuple:
    if version_name and version_code:
        normalized_name = version_name
        normalized_code = version_name_to_code(version_name)
        if normalized_code != re.sub(r"\D", "", version_code).zfill(3):
            raise ValueError(
                f"版本名称 {version_name} 与版本号 {version_code} 不一致，"
                f"按规则应分别为 {version_name} 和 {normalized_code}。"
            )
        return normalized_name, normalized_code

    if version_name:
        return version_name, version_name_to_code(version_name)

    if version_code:
        normalized_code = re.sub(r"\D", "", version_code).zfill(3)
        return version_code_to_name(normalized_code), normalized_code

    raise ValueError("未识别到版本信息，请按“版本：0.0.1”或“版本号：001”格式输入。")


def derive_version_code(version_name: str, explicit_version_code: str = "") -> str:
    _, version_code = resolve_version_pair(version_name, explicit_version_code)
    return version_code


def extract_product_name(text: str) -> str:
    markers = [
        "辛苦出",
        "版本：",
        "版本:",
        "渠道号：",
        "渠道号:",
        "备案号",
        "打包路径",
        "带gromore",
        "gromore",
        "需加混淆",
        "加垃圾",
        "垃圾代码",
        "不混淆",
        "不要混淆",
    ]
    end_index = len(text)
    for marker in markers:
        idx = text.find(marker)
        if idx != -1:
            end_index = min(end_index, idx)

    head = text[:end_index].strip()
    candidates = [item.strip() for item in re.split(r"[\s，,。；;【】\[\]\(\)（）]+", head) if item.strip()]
    if not candidates:
        raise ValueError("未识别到产品名称，请把产品名称放在文案靠前位置。")

    product_name = candidates[0]
    product_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", product_name)
    if not product_name:
        raise ValueError("未识别到有效的产品名称，请检查输入文案。")
    return product_name


def to_full_pinyin(text: str) -> str:
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError as exc:
        raise RuntimeError(
            "未安装 pypinyin。请先执行 `pip install -r requirements.txt --break-system-packages`。"
        ) from exc

    syllables = lazy_pinyin(text, style=Style.NORMAL, errors=lambda item: [item])
    merged = "".join(syllables).lower()
    merged = re.sub(r"[^a-z0-9]+", "", merged)
    if not merged:
        raise ValueError("产品名称无法转换为全拼，请检查输入文案。")
    return merged


def build_parameters(parsed: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, str]:
    defaults = dict(config.get("parameter_defaults", {}))

    defaults["BRANCH"] = parsed["source_branch"]
    defaults["CHANNELS"] = parsed["channel"]
    defaults["version_name"] = parsed["version_name"]
    defaults["version_code"] = parsed["version_code"]
    defaults["enableProguard"] = "true" if parsed["enable_proguard"] else "false"
    defaults["junkcode"] = "true" if parsed["enable_junkcode"] else "false"
    defaults["init_ks_sdk_when_lock"] = parsed["init_ks_sdk_when_lock"]
    defaults["market_channel_ui"] = parsed["market_channel_ui"]

    cocos_path = str(parsed.get("cocos_path") or "").strip()
    if cocos_path:
        defaults["cocos_path"] = cocos_path

    if parsed.get("is_gromore"):
        defaults["adSdkType"] = "Gromore"

    if parsed["package_mode"] == "pure_b":
        defaults["lib_a_include_switch"] = "0"
    else:
        defaults["lib_a_include_switch"] = "1"

    defaults["use_a_launch_theme"] = "false"
    defaults["use_cocos_a"] = "false"

    if parsed["package_mode"] in {"a_plus_b", "pure_b"}:
        defaults["has_cocos"] = "true"
    elif parsed["package_mode"] == "a_plus_b_no_game":
        defaults["has_cocos"] = "false"

    mixed_or_b_modes = {"a_plus_b", "pure_b", "a_plus_b_no_game"}
    if parsed["package_mode"] in mixed_or_b_modes:
        defaults["strongProguard"] = ""

    cleared_params = {"strongProguard"} if parsed["package_mode"] in mixed_or_b_modes else set()
    result: Dict[str, str] = {}
    for key, value in defaults.items():
        if value is None:
            continue
        if str(value) == "" and key not in cleared_params:
            continue
        result[key] = str(value)
    return result


def parse_request(text: str, config: Dict[str, Any]) -> ParsedRequest:
    normalized = normalize_text(text)
    product_name = extract_product_name(normalized)
    source_branch = to_full_pinyin(product_name)
    package_mode = detect_package_mode(normalized)

    version_name = extract_first(
        [
            r"版本(?:名称)?[:：]\s*([0-9]+(?:\.[0-9]+){0,3})",
            r"version[_\s-]*name[:：]\s*([0-9]+(?:\.[0-9]+){0,3})",
        ],
        normalized,
        re.IGNORECASE,
    )

    explicit_version_code = extract_first(
        [
            r"version[_\s-]*code[:：]\s*([0-9]+)",
            r"版本号[:：]\s*([0-9]+)",
        ],
        normalized,
        re.IGNORECASE,
    )

    if not version_name and not explicit_version_code:
        raise ValueError("未识别到版本信息，请按“版本：0.0.1”或“版本号：001”格式输入。")

    version_name, version_code = resolve_version_pair(version_name, explicit_version_code)

    channel = extract_first(
        [
            r"渠道号[:：]\s*([A-Za-z0-9_\-]+)",
            r"渠道[:：]\s*([A-Za-z0-9_\-]+)",
            r"channel[s]?[:：]\s*([A-Za-z0-9_\-]+)",
        ],
        normalized,
        re.IGNORECASE,
    )
    if not channel:
        raise ValueError("未识别到渠道号，请按“渠道号：qzcxryxiaomi”这种格式输入。")

    enable_proguard, enable_junkcode = detect_proguard_and_junkcode(normalized)
    use_a_icon = detect_use_a_icon(normalized)
    market_channel_ui = detect_market_channel_ui(normalized)
    is_gromore = detect_gromore(normalized)
    init_ks_sdk_when_lock = detect_init_ks_sdk_when_lock(normalized)
    cocos_path = detect_cocos_path(normalized)

    # 带 gromore 走「混淆2-自动换分支」，并通过 adSdkType=Gromore 区分（纯 A 不会带 gromore）
    if package_mode == "pure_a":
        job_name = config["jobs"]["pure_a"]
    else:
        job_name = config["jobs"]["mixed_or_b"]

    parsed_dict = {
        "raw_text": normalized,
        "product_name": product_name,
        "source_branch": source_branch,
        "job_name": job_name,
        "package_mode": package_mode,
        "version_name": version_name,
        "version_code": version_code,
        "channel": channel,
        "enable_proguard": enable_proguard,
        "enable_junkcode": enable_junkcode,
        "use_a_icon": use_a_icon,
        "market_channel_ui": market_channel_ui,
        "init_ks_sdk_when_lock": init_ks_sdk_when_lock,
        "cocos_path": cocos_path,
        "is_gromore": is_gromore,
    }
    parameters = build_parameters(parsed_dict, config)

    return ParsedRequest(
        raw_text=normalized,
        product_name=parsed_dict["product_name"],
        source_branch=parsed_dict["source_branch"],
        job_name=parsed_dict["job_name"],
        package_mode=parsed_dict["package_mode"],
        version_name=parsed_dict["version_name"],
        version_code=parsed_dict["version_code"],
        channel=parsed_dict["channel"],
        enable_proguard=parsed_dict["enable_proguard"],
        enable_junkcode=parsed_dict["enable_junkcode"],
        use_a_icon=parsed_dict["use_a_icon"],
        parameters=parameters,
    )


def apply_parameter_overrides(parsed: ParsedRequest, overrides: Dict[str, Any]) -> ParsedRequest:
    if not overrides:
        return parsed
    merged = dict(parsed.parameters)
    for key, value in overrides.items():
        if value is None:
            continue
        merged[str(key)] = str(value)
    parsed.parameters = merged
    return parsed


def find_parameter_block(page: Any, field_name: str) -> Any:
    safe_name = field_name.replace("'", "\\'")
    xpaths = [
        f"xpath=//div[@name='parameter'][.//input[@name='name' and @value='{safe_name}']]",
        f"xpath=//div[contains(@class, 'active-choice')][.//input[@name='name' and @value='{safe_name}']]",
        f"xpath=//div[contains(@class, 'jenkins-select')][.//input[@name='name' and @value='{safe_name}']]",
    ]

    for expr in xpaths:
        locator = page.locator(expr)
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def find_parameter_value_control(page: Any, field_name: str) -> Any:
    block = find_parameter_block(page, field_name)
    if block is None:
        return None

    for selector in ("select[name='value']", "textarea[name='value']", "input[name='value']"):
        locator = block.locator(selector)
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def find_branch_search_control(page: Any, field_name: str) -> Any:
    block = find_parameter_block(page, field_name)
    if block is None:
        return None

    locator = block.locator("input[name='test']")
    try:
        if locator.count() > 0:
            return locator.first
    except Exception:
        return None
    return None


def find_branch_select_control(page: Any, field_name: str) -> Any:
    block = find_parameter_block(page, field_name)
    if block is None:
        return None

    locator = block.locator("select[name='value']")
    try:
        if locator.count() > 0:
            return locator.first
    except Exception:
        return None
    return None


def fill_branch_search_control(page: Any, field_name: str, pinyin: str) -> None:
    search = find_branch_search_control(page, field_name)
    select = find_branch_select_control(page, field_name)
    if search is None or select is None:
        raise RuntimeError(f"未找到 {field_name} 的分支搜索框或下拉框。")

    search.scroll_into_view_if_needed()
    search.click()
    search.fill("")
    search.press_sequentially(pinyin, delay=80)
    time.sleep(1.5)

    option_selectors = [
        f"option[value$='/{pinyin}']",
        f"option:text-matches('.*/{re.escape(pinyin)}$', 'i')",
        f"option[value*='{pinyin}']",
    ]
    for selector in option_selectors:
        option = select.locator(selector)
        try:
            if option.count() > 0:
                value = option.first.get_attribute("value") or ""
                if value:
                    select.select_option(value)
                    time.sleep(0.5)
                    return
        except Exception:
            continue

    visible_options = select.locator("option")
    try:
        if visible_options.count() == 1:
            value = visible_options.first.get_attribute("value") or ""
            if value:
                select.select_option(value)
                return
    except Exception:
        pass

    raise RuntimeError(f"未在 Jenkins 分支列表中找到拼音为 {pinyin} 的分支。")


def find_parameter_control(page: Any, field_name: str) -> Any:
    control = find_parameter_value_control(page, field_name)
    if control is not None:
        return control

    safe_name = field_name.replace("'", "\\'")
    xpaths = [
        f"//div[.//*[normalize-space(text())='{safe_name}']]//*[self::input or self::textarea or self::select][not(@type='hidden')][1]",
        f"//tr[.//*[normalize-space(text())='{safe_name}']]//*[self::input or self::textarea or self::select][not(@type='hidden')][1]",
        f"//*[normalize-space(text())='{safe_name}']/following::input[1]",
        f"//*[normalize-space(text())='{safe_name}']/following::textarea[1]",
        f"//*[normalize-space(text())='{safe_name}']/following::select[1]",
    ]

    for expr in xpaths:
        locator = page.locator(f"xpath={expr}")
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def fill_control(control: Any, value: str) -> None:
    tag_name = control.evaluate("el => el.tagName.toLowerCase()")
    input_type = control.evaluate("el => (el.getAttribute('type') || '').toLowerCase()")

    if tag_name == "select":
        try:
            control.select_option(value=str(value))
        except Exception:
            control.select_option(label=str(value))
        return

    if input_type in {"checkbox", "radio"}:
        expected = str(value).lower() in {"1", "true", "yes", "on"}
        if control.is_checked() != expected:
            control.click()
        return

    control.fill(str(value))


def click_first(page: Any, selectors: List[str]) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                locator.first.click()
                return True
        except Exception:
            continue
    return False


def click_build_button(page: Any) -> None:
    selectors = [
        "form[name='parameters'] button.jenkins-button--primary",
        "form[name='parameters'] button.jenkins-!-build-color",
        "form[name='parameters'] button:has-text('Build')",
        "form[name='parameters'] button:has-text('构建')",
        "form[name='parameters'] input[type='submit']",
        "button.jenkins-!-build-color:has-text('Build')",
        "button:has-text('Build')",
        "button:has-text('开始构建')",
        "button:has-text('立即构建')",
        "button:has-text('构建')",
    ]

    last_error: Optional[Exception] = None
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            button = locator.first
            button.scroll_into_view_if_needed(timeout=5000)
            button.click(timeout=5000)
            return
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        raise RuntimeError("未找到构建提交按钮，请手动检查 Jenkins 页面。") from last_error
    raise RuntimeError("未找到构建提交按钮，请手动检查 Jenkins 页面。")


def is_login_page(page: Any) -> bool:
    current_url = (page.url or "").lower()
    return "login" in current_url or "signin" in current_url


def resolve_user_data_dir(config: Dict[str, Any]) -> Path:
    browser_config = config.get("browser", {})
    raw_dir = browser_config.get("user_data_dir", ".playwright_profile")
    path = Path(raw_dir).expanduser()
    if not path.is_absolute():
        path = (SCRIPT_DIR / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def wait_for_manual_login(page: Any, timeout_ms: int) -> None:
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        if not is_login_page(page):
            return
        time.sleep(1)
    raise RuntimeError("登录超时：请先在打开的浏览器里完成 Jenkins 登录，然后重新点击「开始打包」。")


def ensure_login(page: Any, config: Dict[str, Any], headed: bool = False) -> None:
    base_url = config["jenkins"]["base_url"].rstrip("/")
    page.goto(base_url, wait_until="domcontentloaded")

    if not is_login_page(page):
        return

    username_env = config["jenkins"].get("username_env", "JENKINS_USERNAME")
    password_env = config["jenkins"].get("password_env", "JENKINS_PASSWORD")
    username = os.getenv(username_env, "")
    password = os.getenv(password_env, "")
    browser_config = config.get("browser", {})
    allow_manual_login = browser_config.get("manual_login_if_needed", True)
    manual_login_timeout_ms = int(browser_config.get("manual_login_timeout_ms", 180000))

    if username and password:
        page.locator("input[name='j_username'], input[name='username']").first.fill(username)
        page.locator("input[name='j_password'], input[name='password']").first.fill(password)
        page.locator("button[type='submit'], input[type='submit']").first.click()
        page.wait_for_load_state("networkidle")
        if not is_login_page(page):
            return

    if allow_manual_login and headed:
        print("检测到 Jenkins 登录页，请在打开的浏览器中手动登录一次，登录态会保存在本地资料目录。")
        wait_for_manual_login(page, manual_login_timeout_ms)
        page.wait_for_load_state("networkidle")
        return

    raise RuntimeError(
        "检测到 Jenkins 需要登录。请先勾选「可视化打开浏览器执行」，"
        "在弹出浏览器中手动登录一次；登录成功后，后续可不勾选直接提交。"
    )


def normalize_job_name(job_name: str) -> str:
    return job_name.replace(" | ", "｜").replace("|", "｜").replace(" ", "")


def build_job_page_urls(base_url: str, job_name: str) -> List[str]:
    candidates = []
    for name in (job_name, normalize_job_name(job_name)):
        if name not in candidates:
            candidates.append(name)

    return [f"{base_url}/job/{quote(name, safe='')}/build?delay=0sec" for name in candidates]


def open_build_page(page: Any, config: Dict[str, Any], job_name: str) -> None:
    base_url = config["jenkins"]["base_url"].rstrip("/")
    last_url = ""

    for build_url in build_job_page_urls(base_url, job_name):
        last_url = build_url
        page.goto(build_url, wait_until="networkidle")

        if is_login_page(page):
            return

        if page.locator("form[name='parameters']").count() > 0:
            return

    build_selectors = [
        "a:has-text('Build with Parameters')",
        "a:has-text('参数化构建')",
        "button:has-text('Build with Parameters')",
        "button:has-text('参数化构建')",
    ]
    if click_first(page, build_selectors):
        page.wait_for_load_state("networkidle")
        if page.locator("form[name='parameters']").count() > 0:
            return

    raise RuntimeError(
        f"未能打开任务 {job_name} 的参数化构建页面。"
        f"请确认 config.json 中任务名与 Jenkins 完全一致（注意全角符号 ｜）。"
        f"最后访问：{last_url or page.url}"
    )


def resolve_browser_channel(config: Dict[str, Any]) -> str:
    """返回 Playwright channel。chrome=系统 Google Chrome；空=自带 Chrome for Testing。"""
    return str(config.get("browser", {}).get("channel", "") or "").strip()


def resolve_playwright_browsers_path(config: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    # 使用系统 Chrome 时不依赖 Playwright 自带的 Chromium / Chrome for Testing
    if config is not None and resolve_browser_channel(config) in {"chrome", "chrome-beta", "msedge"}:
        return None

    def has_chromium_bundle(path: Path) -> bool:
        if not path.is_dir():
            return False
        patterns = [
            "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app",
            "chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell",
        ]
        return any(path.glob(pattern) for pattern in patterns)

    candidates: List[Path] = []
    env_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())

    candidates.extend(
        [
            Path.home() / "Library/Caches/ms-playwright",
            Path.home() / ".cache/ms-playwright",
        ]
    )

    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if has_chromium_bundle(resolved):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(resolved)
            return resolved

    raise RuntimeError(
        "未找到 Playwright 浏览器。请先执行：\n"
        "python3 -m playwright install chromium\n"
        "或在 config.json 的 browser.channel 设为 chrome，改用系统 Google Chrome。"
    )


def is_profile_in_use(user_data_dir: Path) -> bool:
    marker = f"user-data-dir={user_data_dir}"
    try:
        result = subprocess.run(
            ["pgrep", "-f", marker],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return False
    return bool(result.stdout.strip())


def _remove_profile_lock_files(user_data_dir: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = user_data_dir / name
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except Exception:
            continue


def _browser_session_is_alive() -> bool:
    context = _BROWSER_SESSION.get("context")
    if context is None:
        return False
    try:
        pages = context.pages
        if not pages:
            return False
        _ = pages[0].url
        return True
    except Exception:
        return False


def _should_reset_browser_session(exc: Optional[BaseException] = None) -> bool:
    if exc is not None:
        message = str(exc).lower()
        # 浏览器可执行文件缺失等安装问题不应自动重试，避免掩盖真实错误
        if "executable doesn't exist" in message or "playwright install" in message:
            return False
        reset_markers = (
            "cannot switch to a different thread",
            "which happens to have exited",
            "target closed",
            "browser has been closed",
            "context has been closed",
            "connection closed",
            "has been closed",
            "asyncio loop",
            "use the async api instead",
            "sync api inside",
        )
        if any(marker in message for marker in reset_markers):
            return True
    return not _browser_session_is_alive()


def _prepare_browser_thread_event_loop() -> None:
    """Playwright Sync API 不能在「正在运行」的 asyncio 循环里调用，线程启动时准备一个空闲 loop。"""
    try:
        import asyncio

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running.is_running():
            # 理论上工作线程不应有 running loop；若有则换新 loop（不 start）
            pass

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    except Exception:
        pass


def _browser_executor_loop() -> None:
    _prepare_browser_thread_event_loop()
    while True:
        item = _BROWSER_EXEC_QUEUE.get()
        if item is None:
            break
        fn, args, kwargs, future = item
        if future.cancelled():
            continue
        if not future.set_running_or_notify_cancel():
            continue
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)


def _ensure_browser_executor() -> None:
    global _BROWSER_EXEC_THREAD
    with _BROWSER_EXEC_LOCK:
        if _BROWSER_EXEC_THREAD is not None and _BROWSER_EXEC_THREAD.is_alive():
            return
        _BROWSER_EXEC_THREAD = threading.Thread(
            target=_browser_executor_loop,
            name="playwright-browser-worker",
            daemon=True,
        )
        _BROWSER_EXEC_THREAD.start()


def run_in_browser_thread(fn, *args, **kwargs):
    _ensure_browser_executor()
    future: Future = Future()
    _BROWSER_EXEC_QUEUE.put((fn, args, kwargs, future))
    return future.result()


def _destroy_browser_session_unlocked() -> None:
    context = _BROWSER_SESSION.get("context")
    playwright = _BROWSER_SESSION.get("playwright")
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            playwright.stop()
        except Exception:
            pass
    _BROWSER_SESSION["playwright"] = None
    _BROWSER_SESSION["context"] = None
    _BROWSER_SESSION["user_data_dir"] = None
    _BROWSER_SESSION["headed"] = None


def destroy_browser_session() -> None:
    run_in_browser_thread(_destroy_browser_session_unlocked)


def reclaim_profile_if_orphaned(user_data_dir: Path) -> None:
    if not is_profile_in_use(user_data_dir):
        _remove_profile_lock_files(user_data_dir)
        return

    if _browser_session_is_alive() and _BROWSER_SESSION.get("user_data_dir") == user_data_dir:
        return

    marker = f"user-data-dir={user_data_dir}"
    subprocess.run(["pkill", "-f", marker], check=False)
    time.sleep(1)
    _remove_profile_lock_files(user_data_dir)


def acquire_browser_session(config: Dict[str, Any], headed: bool, user_data_dir: Path) -> tuple:
    from playwright.sync_api import sync_playwright

    with _BROWSER_SESSION_LOCK:
        if (
            _browser_session_is_alive()
            and _BROWSER_SESSION.get("user_data_dir") == user_data_dir
            and _BROWSER_SESSION.get("headed") == headed
        ):
            return _BROWSER_SESSION["playwright"], _BROWSER_SESSION["context"], True

        _destroy_browser_session_unlocked()
        reclaim_profile_if_orphaned(user_data_dir)

        if is_profile_in_use(user_data_dir):
            raise RuntimeError(
                "检测到已有浏览器占用 Jenkins 登录配置。"
                "如不是本服务打开的窗口，请先关闭后重试。"
            )

        playwright = sync_playwright().start()
        try:
            # channel=chrome 时使用系统 Google Chrome；未配置则用 Playwright 自带 Chrome for Testing
            launch_kwargs = {
                "user_data_dir": str(user_data_dir),
                "headless": not headed,
            }
            channel = resolve_browser_channel(config)
            if channel:
                launch_kwargs["channel"] = channel
            elif not headed:
                # 无 channel 时强制不走 chromium_headless_shell，复用已安装的 chromium 包
                os.environ["PLAYWRIGHT_CHROMIUM_USE_HEADLESS_SHELL"] = "0"
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            try:
                playwright.stop()
            except Exception:
                pass
            raise
        _BROWSER_SESSION["playwright"] = playwright
        _BROWSER_SESSION["context"] = context
        _BROWSER_SESSION["user_data_dir"] = user_data_dir
        _BROWSER_SESSION["headed"] = headed
        return playwright, context, False


def cleanup_stale_profile_locks(user_data_dir: Path) -> None:
    if is_profile_in_use(user_data_dir):
        if _browser_session_is_alive() and _BROWSER_SESSION.get("user_data_dir") == user_data_dir:
            return
        raise RuntimeError(
            "检测到上一次打开的浏览器仍在运行。请先关闭弹出的 Chrome 窗口，"
            "等待当前打包结束后再重试。"
        )

    _remove_profile_lock_files(user_data_dir)


def trigger_build(config: Dict[str, Any], parsed: ParsedRequest, headed: bool, screenshot_path: str = "") -> None:
    return run_in_browser_thread(_execute_trigger_build, config, parsed, headed, screenshot_path)


def _execute_trigger_build(
    config: Dict[str, Any], parsed: ParsedRequest, headed: bool, screenshot_path: str = ""
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "未安装 playwright。请先执行 `pip install -r requirements.txt`，再执行 "
            "`python -m playwright install chromium`。"
        ) from exc

    resolve_playwright_browsers_path(config)
    user_data_dir = resolve_user_data_dir(config)
    should_keep_open = headed and config.get("browser", {}).get("keep_open_after_build", True)

    try:
        for attempt in range(2):
            try:
                _run_trigger_build_once(config, parsed, headed, screenshot_path, user_data_dir, should_keep_open)
                return
            except Exception as exc:
                if attempt == 0 and _should_reset_browser_session(exc):
                    print("检测到浏览器窗口已关闭，正在重新打开...")
                    with _BROWSER_SESSION_LOCK:
                        _destroy_browser_session_unlocked()
                    reclaim_profile_if_orphaned(user_data_dir)
                    continue
                raise
    finally:
        if not should_keep_open:
            with _BROWSER_SESSION_LOCK:
                _destroy_browser_session_unlocked()


def _run_trigger_build_once(
    config: Dict[str, Any],
    parsed: ParsedRequest,
    headed: bool,
    screenshot_path: str,
    user_data_dir: Path,
    should_keep_open: bool,
) -> None:
    playwright, context, reused_session = acquire_browser_session(config, headed, user_data_dir)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(config.get("playwright_timeout_ms", 15000))

        ensure_login(page, config, headed=headed)
        open_build_page(page, config, parsed.job_name)

        branch_config = config.get("branch_parameter", {})
        branch_param_name = branch_config.get("name", "BRANCH")
        branch_fill_mode = branch_config.get("fill_mode", "pinyin_search")

        not_found = []
        for key, value in parsed.parameters.items():
            if key == branch_param_name and branch_fill_mode == "pinyin_search":
                fill_branch_search_control(page, key, value)
                continue

            control = find_parameter_control(page, key)
            if control is None:
                not_found.append(key)
                continue
            fill_control(control, value)

        if not_found:
            print("以下参数未在页面中定位到，请检查 Jenkins 页面参数名是否与配置一致：")
            for item in not_found:
                print(f"- {item}")

        click_build_button(page)

        page.wait_for_load_state("networkidle")
        if screenshot_path:
            page.screenshot(path=screenshot_path, full_page=True)
        print("构建已提交。")
        print(f"任务：{parsed.job_name}")
        print(f"页面：{page.url}")
        if should_keep_open:
            if reused_session:
                print("已复用浏览器窗口，可直接发起下一次打包。")
            else:
                print("浏览器窗口将保持打开，后续打包会自动复用该窗口。")
    except Exception:
        if not should_keep_open:
            with _BROWSER_SESSION_LOCK:
                _destroy_browser_session_unlocked()
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据中文打包要求自动触发 Jenkins 打包。")
    parser.add_argument("--config", default="config.json", help="配置文件路径，默认读取当前目录下的 config.json")
    parser.add_argument("--text", help="直接传入一整段打包要求文本")
    parser.add_argument("--text-file", help="从文件读取打包要求文本")
    parser.add_argument("--dry-run", action="store_true", help="只解析参数，不真正打开 Jenkins")
    parser.add_argument("--headed", action="store_true", help="可视化打开浏览器，便于观察自动化过程")
    parser.add_argument("--screenshot", default="", help="构建提交后的截图保存路径")
    return parser.parse_args()


def read_request_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    raise ValueError("请通过 --text 或 --text-file 提供打包要求文本。")


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    text = read_request_text(args)
    parsed = parse_request(text, config)

    print(json.dumps(asdict(parsed), ensure_ascii=False, indent=2))

    if args.dry_run:
        return 0

    trigger_build(config=config, parsed=parsed, headed=args.headed, screenshot_path=args.screenshot)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
