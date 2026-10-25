"""52Pokemon 入门精灵球续费与兑换助手。

创建订单前需要显式传入 --confirm-order；脚本不会自动执行支付。
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait


DEFAULT_BASE_URL = "https://web1.52pokemon.cc"
BASE_URL = os.environ.get("POKEMON_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
PROJECT_DIR = Path(__file__).resolve().parent


def _default_chrome_binary() -> str:
    configured = os.environ.get("POKEMON_CHROME_BINARY", "").strip()
    if configured:
        return configured
    if sys.platform.startswith("win"):
        return r"D:\ocs\OCS Desktop\resources\bin\chrome\chrome\chrome.exe"
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _find_driver() -> str:
    configured = os.environ.get("POKEMON_CHROMEDRIVER", "").strip()
    if configured:
        return configured
    candidates = (
        PROJECT_DIR / "chromedriver.exe",
        PROJECT_DIR
        / "vendor"
        / "chromedriver-extracted"
        / "chromedriver-win64"
        / "chromedriver.exe",
        PROJECT_DIR / "chromedriver",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


CHROME_BINARY = _default_chrome_binary()
CHROMEDRIVER = _find_driver()
PLAN_NAME = "入门精灵球"
DEFAULT_WAIT_SECONDS = 30
LOGIN_WAIT_SECONDS = int(os.environ.get("POKEMON_LOGIN_WAIT", "300"))
LOGIN_ATTEMPT_WAIT_SECONDS = int(
    os.environ.get("POKEMON_LOGIN_ATTEMPT_WAIT", "25")
)
LOGIN_ATTEMPTS = max(1, int(os.environ.get("POKEMON_LOGIN_ATTEMPTS", "3")))
STEP_DELAY_SECONDS = float(os.environ.get("POKEMON_STEP_DELAY", "1.5"))
PROFILE_DIR = Path(
    os.environ.get(
        "POKEMON_PROFILE_DIR",
        str(Path(__file__).resolve().parent / ".chrome-profile-auto"),
    )
)
SECURITY_MARKERS = (
    "这不是安全连接",
    "您的连接不是私密连接",
    "隐私错误",
    "your connection is not private",
    "net::err_cert",
)
SECURITY_VERIFICATION_MARKERS = (
    "Security Verification",
    "slider_html",
    "puzzle-container",
    "滑块",
    "安全验证",
    "人机验证",
)
LOGIN_ERROR_MARKERS = (
    "密码错误",
    "账号或密码错误",
    "验证码错误",
    "登录失败",
)


class PurchaseFailure(Exception):
    """A classified failure that the pool runner can record without credentials."""

    def __init__(self, message: str, code: str = "purchase_failed", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def set_base_url(value: str) -> str:
    """Change the site origin for one run without editing the workflow."""
    global BASE_URL
    candidate = value.strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PurchaseFailure(
            "网站基础地址必须是 http:// 或 https:// 开头的完整地址",
            code="invalid_base_url",
            retryable=False,
        )
    BASE_URL = candidate
    return BASE_URL
PAGE_ERROR_MARKERS = (
    "无法访问此站点",
    "502 Bad Gateway",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_INTERNET_DISCONNECTED",
)


def pause(label: str) -> None:
    """给页面渲染、接口响应和人工操作留出可观察的间隔。"""
    if STEP_DELAY_SECONDS <= 0:
        return
    print(f"{label}，等待 {STEP_DELAY_SECONDS:g} 秒。")
    time.sleep(STEP_DELAY_SECONDS)


def save_page_screenshot(driver: webdriver.Chrome, label: str) -> Path | None:
    """Save a browser screenshot without masking the original script failure."""
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "page"
    screenshot_dir = PROJECT_DIR / "screenshots"
    screenshot_path = screenshot_dir / f"{safe_label}-{time.time_ns()}.png"
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(screenshot_path))
        print(f"页面截图：{screenshot_path}")
        return screenshot_path
    except Exception as exc:
        print(f"页面截图失败：{exc.__class__.__name__}")
        return None


def save_browser_diagnostics(driver: webdriver.Chrome) -> Path | None:
    """Save authentication/network error lines without form values."""
    try:
        entries = driver.get_log("browser")
    except Exception:
        return None
    lines = [
        f"{entry.get('level', 'INFO')} {entry.get('message', '')}"
        for entry in entries
        if any(
            marker in entry.get("message", "")
            for marker in (
                "auth",
                "login",
                "passport",
                "Failed to load resource",
                "ERR_",
                "500",
            )
        )
    ]
    if not lines:
        return None
    path = PROJECT_DIR / "screenshots" / f"browser-{time.time_ns()}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"浏览器诊断：{path}")
    return path


def save_navigation_diagnostics(driver: webdriver.Chrome) -> Path | None:
    """Record links/buttons around the dashboard purchase entry."""
    try:
        items = driver.execute_script(
            """
            return Array.from(document.querySelectorAll('a,button,[role="button"]'))
              .map(e => ({
                tag: e.tagName,
                text: (e.innerText || e.textContent || '').trim(),
                href: e.getAttribute('href') || '',
                aria: e.getAttribute('aria-label') || '',
                role: e.getAttribute('role') || '',
                cls: e.className || ''
              }))
              .filter(x => /套餐|购买|开通|plan|shop|order/i.test(
                x.text + ' ' + x.href + ' ' + x.aria + ' ' + x.cls
              ));
            """
        )
    except Exception:
        return None
    path = PROJECT_DIR / "screenshots" / f"navigation-{time.time_ns()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"导航诊断：{path}")
    return path


def is_security_interstitial(driver: webdriver.Chrome) -> bool:
    """识别 Chrome 证书提示页，不自动绕过它。"""
    try:
        content = " ".join(
            (
                driver.title,
                driver.current_url,
                driver.page_source[:10000],
            )
        ).lower()
    except WebDriverException:
        return False
    return any(marker.lower() in content for marker in SECURITY_MARKERS)


def wait_for_manual_security_action(driver: webdriver.Chrome) -> None:
    """等待用户在 Chrome 安全提示页上手动作出决定。"""
    if not is_security_interstitial(driver):
        return
    if os.environ.get("POKEMON_HEADLESS", "0").lower() in {"1", "true", "yes"}:
        raise PurchaseFailure(
            "无头浏览器遇到安全连接提示，无法人工处理",
            code="security_interstitial",
            retryable=False,
        )

    print(
        "检测到 Chrome 的安全连接提示。请在这个独立 Chrome 窗口中手动选择"
        "“继续访问/前往”，脚本会等待页面恢复后自动继续；不会自动绕过安全提示。"
    )
    try:
        WebDriverWait(driver, LOGIN_WAIT_SECONDS).until(
            lambda current: not is_security_interstitial(current)
        )
    except TimeoutException as exc:
        raise SystemExit(
            "安全连接提示未处理，脚本已停止。请手动处理证书提示后重新运行。"
        ) from exc
    pause("安全提示已处理，页面继续加载")


def is_security_verification(driver: webdriver.Chrome) -> bool:
    try:
        content = " ".join(
            (
                driver.title or "",
                driver.current_url or "",
                driver.page_source[:20000],
            )
        ).lower()
    except WebDriverException:
        return False
    return any(marker.lower() in content for marker in SECURITY_VERIFICATION_MARKERS)


def wait_for_security_verification(driver: webdriver.Chrome) -> None:
    """Allow a visible browser to complete a manual slider verification."""
    if not is_security_verification(driver):
        return
    if os.environ.get("POKEMON_HEADLESS", "0").lower() in {"1", "true", "yes"}:
        raise PurchaseFailure(
            "当前站点要求完成滑块/人机安全验证，GitHub Actions 无头浏览器无法自动通过",
            code="security_verification_required",
            retryable=False,
        )
    print("检测到滑块/人机安全验证，请在浏览器中手动完成，脚本会等待页面恢复。")
    try:
        WebDriverWait(driver, LOGIN_WAIT_SECONDS).until(
            lambda current: not is_security_verification(current)
        )
    except TimeoutException as exc:
        raise PurchaseFailure(
            "安全验证未完成，脚本已停止",
            code="security_verification_timeout",
            retryable=False,
        ) from exc
    pause("安全验证已通过")


def open_page(driver: webdriver.Chrome, url: str, label: str) -> None:
    """打开页面并处理可能出现的安全提示。"""
    driver.get(url)
    pause(label)
    wait_for_manual_security_action(driver)
    wait_for_security_verification(driver)


def setup_driver() -> webdriver.Chrome:
    """启动 Chrome；GitHub Actions 通过 POKEMON_HEADLESS=1 使用无头模式。"""
    options = Options()
    options.add_argument("--window-size=1440,1000")

    # ==========================================
    # 核心修改：忽略证书错误，解决 SSL 拦截问题
    # ==========================================
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--allow-running-insecure-content")
    # Selenium 4 推荐的设置方式，与上面参数双管齐下
    options.accept_insecure_certs = True
    # ==========================================

    profile_dir = PROFILE_DIR.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--no-sandbox")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.set_capability(
        "goog:loggingPrefs",
        {"browser": "ALL", "performance": "ALL"},
    )
    if CHROME_BINARY and os.path.exists(CHROME_BINARY):
        options.binary_location = CHROME_BINARY
    elif CHROME_BINARY:
        raise FileNotFoundError(
            f"找不到 Chrome：{CHROME_BINARY}。请设置 POKEMON_CHROME_BINARY。"
        )

    headless = os.environ.get("POKEMON_HEADLESS", "0").lower() in {"1", "true", "yes"}
    if headless:
        options.add_argument("--headless=new")

    if CHROMEDRIVER and not os.path.exists(CHROMEDRIVER):
        raise FileNotFoundError(
            f"找不到 ChromeDriver：{CHROMEDRIVER}。请把 chromedriver.exe 放到项目目录，"
            "或设置 POKEMON_CHROMEDRIVER。"
        )

    print(f"使用独立 Chrome 配置：{profile_dir}")
    print(f"使用 ChromeDriver：{CHROMEDRIVER or 'Selenium Manager 自动解析'}")
    if CHROMEDRIVER:
        driver = webdriver.Chrome(service=Service(CHROMEDRIVER), options=options)
    else:
        driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)
    return driver


def wait_for_dashboard(driver: webdriver.Chrome, timeout: int = DEFAULT_WAIT_SECONDS) -> None:
    wait = WebDriverWait(driver, timeout)
    wait.until(lambda current: "/dashboard" in current.current_url)


def wait_for_login_result(
    driver: webdriver.Chrome,
    timeout: int | None = None,
) -> None:
    """等待登录跳转，同时识别账号错误、网络错误和站点故障。"""
    deadline = time.monotonic() + (timeout or LOGIN_WAIT_SECONDS)
    while time.monotonic() < deadline:
        if "/login" not in driver.current_url:
            return
        page_text = ""
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            pass
        title = driver.title or ""
        combined = f"{title}\n{page_text}"
        wait_for_security_verification(driver)
        for marker in PAGE_ERROR_MARKERS:
            if marker in combined:
                raise SystemExit(f"站点暂时无法访问：{marker}")
        for marker in LOGIN_ERROR_MARKERS:
            if marker in combined:
                raise SystemExit(f"自动登录失败：{marker}")
        try:
            browser_log = "\n".join(
                entry.get("message", "") for entry in driver.get_log("browser")
            )
        except WebDriverException:
            browser_log = ""
        if "passport/auth/login" in browser_log and "status of 500" in browser_log:
            raise TimeoutException("登录接口返回 HTTP 500")
        time.sleep(0.5)
    raise TimeoutException("等待登录跳转超时")


def is_logged_in(driver: webdriver.Chrome) -> bool:
    """通过受保护的应用布局确认登录，而不是反复校验登录表单。"""
    if "/login" in driver.current_url:
        return False
    try:
        return bool(
            driver.find_elements(By.CSS_SELECTOR, "a[href='/plan'], a[href='/dashboard']")
        )
    except WebDriverException:
        return False


def verify_login(driver: webdriver.Chrome, timeout: int = DEFAULT_WAIT_SECONDS) -> None:
    """等待并校验登录状态，失败时给出明确原因。"""
    try:
        WebDriverWait(driver, timeout).until(is_logged_in)
    except TimeoutException as exc:
        raise SystemExit(
            f"登录校验失败：当前页面为 {driver.current_url}，"
            "未检测到已登录的套餐导航。请检查账号、密码或手动登录。"
        ) from exc
    print("登录校验通过。")


def dismiss_blocking_overlays(driver: webdriver.Chrome) -> None:
    """关闭首页公告等遮罩，避免拦截套餐导航点击。"""
    close_xpath = (
        "//*[self::button or @role='button'][contains(@aria-label, '关闭') or contains(@aria-label, 'close') or "
        "contains(@title, '关闭') or contains(@title, 'Close') or "
        "normalize-space(.)='关闭' or normalize-space(.)='稍后提醒' or "
        "normalize-space(.)='今日不再提醒' or normalize-space(.)='我知道了' or "
        "normalize-space(.)='Skip Intro' or normalize-space(.)='不再提示']"
    )
    for _ in range(8):
        try:
            scrims = driver.find_elements(By.CSS_SELECTOR, ".v-overlay__scrim")
            visible_scrims = [scrim for scrim in scrims if scrim.is_displayed()]
        except WebDriverException:
            pause("页面重绘，重新检查遮罩")
            continue
        if not visible_scrims:
            return

        clicked = False
        try:
            close_buttons = driver.find_elements(By.XPATH, close_xpath)
            visible_buttons = [
                button
                for button in close_buttons
                if button.is_displayed() and button.is_enabled()
            ]
        except WebDriverException:
            visible_buttons = []
        def priority(button):
            try:
                text = (button.text or "").strip()
            except WebDriverException:
                return 3
            if "今日不再提醒" in text or "不再提示" in text:
                return 0
            if text in {"关闭", "稍后提醒", "我知道了", "Skip Intro"}:
                return 1
            return 2

        for button in sorted(visible_buttons, key=priority):
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'}); "
                    "arguments[0].click();",
                    button,
                )
                clicked = True
                break
            except WebDriverException:
                # Vue/Vuetify can replace the overlay while clicking.
                continue
        if clicked:
            pause("已关闭页面遮罩")
            try:
                WebDriverWait(driver, 4).until(
                    lambda current: not any(
                        scrim.is_displayed()
                        for scrim in current.find_elements(
                            By.CSS_SELECTOR, ".v-overlay__scrim"
                        )
                    )
                )
            except (TimeoutException, WebDriverException):
                pass
            continue
        if visible_buttons:
            # Re-query the button on the next pass instead of falling back to Esc.
            pause("遮罩正在重绘，重新定位关闭按钮")
            continue

        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            pause("已尝试按 Esc 关闭页面遮罩")
        except WebDriverException:
            continue

    try:
        scrims = driver.find_elements(By.CSS_SELECTOR, ".v-overlay__scrim")
        if any(scrim.is_displayed() for scrim in scrims):
            raise SystemExit("页面遮罩在多次重试后仍未关闭")
    except WebDriverException:
        pass


def submit_login_form(driver: webdriver.Chrome, email: str, password: str) -> None:
    """原子填写两个字段并点击登录，避开 Vue 重绘造成的重复输入。"""
    email_locator = (
        By.CSS_SELECTOR,
        "input[aria-label='电子邮件地址'], input[type='email']",
    )
    password_locator = (
        By.CSS_SELECTOR,
        "input[aria-label='密码'], input[type='password']",
    )
    submit_script = r"""
        const [emailInput, passwordInput, email, password] = arguments;
        const setValue = (element, value) => {
            const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, 'value'
            ).set;
            setter.call(element, value);
            element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
        };
        setValue(emailInput, email);
        setValue(passwordInput, password);
        const button = Array.from(document.querySelectorAll('button')).find(
            element => element.offsetParent !== null
                && element.textContent.replace(/\s+/g, '') === '登录'
        );
        if (!button) return false;
        button.click();
        return true;
    """
    last_error = None
    for attempt in range(3):
        try:
            email_field, password_field = (
                WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
                    ec.visibility_of_element_located(locator)
                )
                for locator in (email_locator, password_locator)
            )
            clicked = driver.execute_script(
                submit_script,
                email_field,
                password_field,
                email,
                password,
            )
            if not clicked:
                raise TimeoutException("登录按钮不可见")
            print("账号密码已填写并点击登录。")
            return
        except (
            StaleElementReferenceException,
            TimeoutException,
            WebDriverException,
        ) as exc:
            last_error = exc
            if attempt < 2:
                pause("登录表单发生页面重绘，重新提交")
    raise SystemExit("登录表单未能提交。") from last_error


def click_element(driver: webdriver.Chrome, element) -> bool:
    """Try a native click first so Vue Router receives a real pointer event."""
    try:
        element.click()
        return True
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'}); "
                "arguments[0].click();",
                element,
            )
            return True
        except WebDriverException:
            return False


def read_plan_card(driver: webdriver.Chrome, wait: WebDriverWait):
    last_error = None
    for attempt in range(3):
        try:
            card, button = wait.until(find_target_plan_elements)
            price = card.find_element(
                By.XPATH, ".//*[contains(normalize-space(.), '¥')]"
            ).text
            button_text = button.text.strip()
            return card, button, price, button_text
        except (StaleElementReferenceException, WebDriverException) as exc:
            last_error = exc
            if attempt < 2:
                pause("套餐卡片正在重绘，重新定位")
    raise SystemExit("套餐卡片在页面重绘期间持续失效") from last_error


def find_target_plan_elements(driver: webdriver.Chrome, action: str | None = None):
    """寻找最小的目标套餐卡片，避免误选同组中的“购前须知”。"""
    action_xpath = (
        ".//button[contains(normalize-space(.), '立即订购') or "
        "contains(normalize-space(.), '续费套餐')]"
    )
    try:
        headings = driver.find_elements(
            By.XPATH,
            f"//*[normalize-space()='{PLAN_NAME}']",
        )
    except WebDriverException:
        return False
    candidates = []
    for heading in headings:
        try:
            ancestors = heading.find_elements(By.XPATH, "ancestor::*")
        except WebDriverException:
            continue
        for ancestor in ancestors:
            try:
                if not ancestor.is_displayed():
                    continue
                for button in ancestor.find_elements(By.XPATH, action_xpath):
                    if not button.is_displayed() or not button.is_enabled():
                        continue
                    button_text = button.text.strip()
                    if action is not None and button_text != action:
                        continue
                    candidates.append((len(ancestor.text), ancestor, button))
            except WebDriverException:
                # The plan page can re-render while walking the card tree.
                continue

    if not candidates:
        return False
    _, card, button = min(candidates, key=lambda item: item[0])
    return card, button


def login_if_needed(driver: webdriver.Chrome, non_interactive: bool = False) -> None:
    """使用环境变量登录；没有环境变量时交给用户在可见浏览器中登录。"""
    open_page(driver, f"{BASE_URL}/login", "登录页已打开")

    if "/dashboard" in driver.current_url:
        verify_login(driver)
        print("已检测到登录会话。")
        return

    email = os.environ.get("POKEMON_EMAIL")
    password = os.environ.get("POKEMON_PASSWORD")
    if not email or not password:
        if non_interactive:
            raise PurchaseFailure(
                "非交互运行缺少 POKEMON_EMAIL 或 POKEMON_PASSWORD",
                code="missing_credentials",
                retryable=False,
            )
        print("请在打开的 Chrome 页面中完成登录；脚本会等待登录成功。")
        wait_for_dashboard(driver, timeout=LOGIN_WAIT_SECONDS)
        pause("手动登录成功")
        verify_login(driver)
        return

    last_error: Exception | None = None
    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        if attempt > 1:
            open_page(driver, f"{BASE_URL}/login", "登录页重新打开")
            print(f"登录未跳转，重新提交（第 {attempt}/{LOGIN_ATTEMPTS} 次）。")
        submit_login_form(driver, email, password)
        pause("已点击登录，等待跳转")
        try:
            wait_for_login_result(
                driver,
                timeout=LOGIN_ATTEMPT_WAIT_SECONDS,
            )
            last_error = None
            break
        except TimeoutException as exc:
            last_error = exc
            if attempt < LOGIN_ATTEMPTS:
                pause("登录接口未跳转，准备重试")
                continue
            raise
    if last_error is not None:
        raise last_error
    pause("登录成功")
    verify_login(driver)
    print("登录成功。")


def open_target_plan(driver: webdriver.Chrome):
    """从已登录首页点击套餐导航，返回目标套餐卡片与按钮。"""
    wait = WebDriverWait(driver, DEFAULT_WAIT_SECONDS)

    if "/dashboard" not in driver.current_url:
        open_page(driver, f"{BASE_URL}/dashboard", "首页已打开")
    dismiss_blocking_overlays(driver)

    plan_link_xpath = (
        "//a[contains(@href, '/plan') and "
        "(normalize-space(.)='套餐购买' or contains(normalize-space(.), '套餐购买'))]"
    )
    navigation_error = None
    for navigation_attempt in range(3):
        for click_attempt in range(4):
            try:
                plan_link = wait.until(
                    ec.element_to_be_clickable((By.XPATH, plan_link_xpath))
                )
                if not click_element(driver, plan_link):
                    raise StaleElementReferenceException("套餐入口点击失败")
                break
            except (
                ElementClickInterceptedException,
                StaleElementReferenceException,
                WebDriverException,
            ):
                if click_attempt == 3:
                    raise SystemExit(
                        "套餐购买链接持续被页面重绘或遮罩拦截，未进入套餐页。"
                    )
                dismiss_blocking_overlays(driver)
        pause("已点击套餐购买，等待页面加载")
        try:
            wait.until(
                lambda current: "/dashboard" not in current.current_url
                and bool(find_target_plan_elements(current))
            )
            navigation_error = None
            break
        except TimeoutException as exc:
            navigation_error = exc
            if "/dashboard" in driver.current_url and navigation_attempt < 2:
                print("套餐导航被重定向回首页，重新点击套餐购买。")
                dismiss_blocking_overlays(driver)
                pause("等待首页稳定后重试套餐导航")
                continue
            break

    if navigation_error is not None:
        print("套餐导航未生效，尝试首页的开通/购买套餐入口。")
        for label in ("开通套餐", "购买套餐"):
            try:
                entries = driver.find_elements(
                    By.XPATH,
                    "//*[self::button or self::a or @role='button']"
                    f"[contains(normalize-space(.), '{label}')]",
                )
                visible_entries = [
                    entry
                    for entry in entries
                    if entry.is_displayed() and entry.is_enabled()
                ]
            except WebDriverException:
                visible_entries = []
            clicked_entry = False
            for entry in reversed(visible_entries):
                if click_element(driver, entry):
                    clicked_entry = True
                    break
            if clicked_entry:
                pause(f"已点击{label}入口")
                try:
                    WebDriverWait(driver, 8).until(
                        lambda current: bool(find_target_plan_elements(current))
                    )
                    navigation_error = None
                    break
                except (TimeoutException, WebDriverException):
                    print(f"{label}入口仍未进入套餐页，继续尝试下一个入口。")

    if navigation_error is not None:
        print("套餐入口点击未生效，尝试直接打开套餐路由。")
        try:
            open_page(driver, f"{BASE_URL}/plan", "套餐页已直接打开")
            dismiss_blocking_overlays(driver)
            WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
                lambda current: "/dashboard" not in current.current_url
                and bool(find_target_plan_elements(current))
            )
            navigation_error = None
        except (TimeoutException, WebDriverException, SystemExit) as exc:
            navigation_error = exc

    if navigation_error is not None:
        screenshot_path = Path(__file__).resolve().parent / "screenshots" / "plan-load-failure.png"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            driver.save_screenshot(str(screenshot_path))
        except WebDriverException:
            pass
        page_text = ""
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            pass
        print(f"套餐页加载失败，当前 URL：{driver.current_url}")
        print(f"截图：{screenshot_path}")
        print(f"页面文本：{page_text[:500]}")
        raise SystemExit(
            f"套餐页未加载成功，当前页面为 {driver.current_url}；"
            "站点可能把套餐路由重定向回首页。"
        ) from navigation_error

    card, button, price, button_text = read_plan_card(driver, wait)
    print(f"已定位套餐：{PLAN_NAME}，页面价格：{price}，操作：{button_text}。")
    return card, button


def find_redeem_input(driver: webdriver.Chrome):
    """定位兑换码输入框，不依赖页面中其他搜索框。"""
    specific_xpath = (
        "//input[not(@type='hidden') and ("
        "contains(@placeholder, '兑换') or contains(@placeholder, '优惠') or "
        "contains(@aria-label, '兑换') or contains(@aria-label, '优惠') or "
        "contains(@name, 'coupon') or contains(@name, 'code') or "
        "contains(@id, 'coupon') or contains(@id, 'code'))]"
    )
    generic_xpath = "//input[not(@type='hidden') and (@type='text' or not(@type))]"

    def locate(current):
        candidates = current.find_elements(By.XPATH, specific_xpath)
        if not candidates:
            candidates = current.find_elements(By.XPATH, generic_xpath)
        for candidate in candidates:
            if candidate.is_displayed() and candidate.is_enabled():
                return candidate
        return False

    return WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(locate)


def apply_redeem_code(driver: webdriver.Chrome, redeem_code: str) -> None:
    """输入兑换码并点击“使用”。"""
    try:
        open_button = WebDriverWait(driver, min(5, DEFAULT_WAIT_SECONDS)).until(
            ec.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[contains(normalize-space(.), '输入兑换码')]",
                )
            )
        )
        open_button.click()
        pause("已点击输入兑换码")
    except TimeoutException:
        pass

    redeem_input = find_redeem_input(driver)
    redeem_input.clear()
    redeem_input.send_keys(redeem_code)
    pause("兑换码已填写")

    use_button = WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
        ec.element_to_be_clickable(
            (
                By.XPATH,
                "//button[normalize-space(.)='使用' or "
                "contains(normalize-space(.), '使用兑换') or "
                "contains(normalize-space(.), '使用优惠')]",
            )
        )
    )
    use_button.click()
    pause("已点击使用，等待兑换结果")


def select_monthly_option(driver: webdriver.Chrome) -> None:
    """选择月付选项，避免页面停留在年付或其他周期。"""
    monthly_option = WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
        ec.element_to_be_clickable(
            (
                By.XPATH,
                "//*[@role='radio'][contains(normalize-space(.), '每月付款') or "
                ".//*[contains(normalize-space(.), '每月付款')] or "
                "contains(normalize-space(.), '月付') or "
                ".//*[contains(normalize-space(.), '月付')]]",
            )
        )
    )
    monthly_option.click()
    pause("已选择月付")


def cancel_old_pending_order(driver: webdriver.Chrome) -> None:
    """下单前取消页面检测到的旧待支付订单。"""
    try:
        WebDriverWait(driver, 5).until(
            lambda current: "检测到待支付订单"
            in current.find_element(By.TAG_NAME, "body").text
        )
    except TimeoutException:
        return

    cancel_button = WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
        ec.element_to_be_clickable(
            (By.XPATH, "//button[normalize-space(.)='取消旧订单']")
        )
    )
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();",
        cancel_button,
    )
    pause("已点击取消旧订单")

    for label in ("确认取消", "确定", "确认"):
        buttons = driver.find_elements(
            By.XPATH,
            f"//button[normalize-space(.)='{label}']",
        )
        visible = [
            button
            for button in buttons
            if button.is_displayed() and button.is_enabled()
        ]
        if visible:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'}); "
                "arguments[0].click();",
                visible[-1],
            )
            pause(f"已点击{label}")
            break

    WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
        lambda current: "检测到待支付订单"
        not in current.find_element(By.TAG_NAME, "body").text
    )
    print("旧待支付订单已取消。")


def wait_for_zero_monthly(driver: webdriver.Chrome):
    """等待页面明确显示月付为 ¥0/¥0.00，并返回对应可见区域。"""
    def locate(current):
        monthly_nodes = current.find_elements(
            By.XPATH,
            "//*[contains(normalize-space(.), '月付')]",
        )
        for node in monthly_nodes:
            if not node.is_displayed():
                continue
            ancestor = node
            for _ in range(6):
                normalized = re.sub(r"\s+", "", ancestor.text)
                if (
                    "月付" in normalized
                    and re.search(r"(?:¥|￥)0(?:\.0+)?", normalized)
                ):
                    return ancestor
                try:
                    ancestor = ancestor.find_element(By.XPATH, "..")
                except Exception:
                    break
        return False

    return WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(locate)


def click_order_after_zero(driver: webdriver.Chrome) -> None:
    """只有确认月付为 0 后，才点击“立即订购”。"""
    order_button = WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
        ec.element_to_be_clickable(
            (
                By.CSS_SELECTOR,
                "button.order-action-btn",
            )
        )
    )

    print("已确认月付为 0，准备点击立即订购。")
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();",
        order_button,
    )
    pause("已点击立即订购，等待订单结果")
    screenshot_path = (
        Path(__file__).resolve().parent / "screenshots" / "order-after-click.png"
    )
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        driver.save_screenshot(str(screenshot_path))
        print(f"订单操作截图：{screenshot_path}")
    except WebDriverException:
        pass
    print(f"点击立即订购后的 URL：{driver.current_url}")
    try:
        WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
            lambda current: bool(re.search(r"/order/\d+", current.current_url))
            or bool(
                re.search(
                    r"订单号[:：]\s*\d+",
                    current.find_element(By.TAG_NAME, "body").text,
                )
            )
        )
    except TimeoutException as exc:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        print(f"下单后页面文本：{body_text[:800]}")
        raise SystemExit("点击立即订购后未检测到订单创建结果。") from exc
    body_text = driver.find_element(By.TAG_NAME, "body").text
    order_match = re.search(r"订单号[:：]\s*(\d+)", body_text)
    order_id = order_match.group(1) if order_match else None
    if not order_id:
        url_match = re.search(r"/order/(\d+)", driver.current_url)
        order_id = url_match.group(1) if url_match else None
    if order_id:
        print(f"新订单号：{order_id}")
    return order_id


def click_plan_action(driver: webdriver.Chrome, action: str) -> None:
    """确认后重新定位套餐按钮，避免页面重绘造成 stale element。"""
    last_error = None
    for attempt in range(3):
        try:
            _card, button = WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
                lambda current: find_target_plan_elements(current, action)
            )
            if not click_element(driver, button):
                raise StaleElementReferenceException("套餐按钮点击失败")
            return
        except (StaleElementReferenceException, WebDriverException) as exc:
            last_error = exc
            if attempt < 2:
                pause("套餐卡片正在刷新，重新定位按钮")
    raise SystemExit("套餐按钮在页面刷新期间持续失效，未执行后续操作。") from last_error


def prepare_order(
    driver: webdriver.Chrome,
    redeem_code: str,
    dry_run: bool = False,
) -> str | None:
    _card, button = open_target_plan(driver)
    action = button.text.strip()
    if action not in {"续费套餐", "立即订购"}:
        raise SystemExit(
            f"目标套餐当前按钮为“{action}”，不是“续费套餐”或“立即订购”；已停止。"
        )
    if action == "续费套餐":
        print("当前账号已有该套餐，后续操作将是续费。")
    else:
        print("当前账号没有该套餐，后续操作将是首次订购。")

    click_plan_action(driver, action)
    pause("已点击套餐操作按钮，等待订单页加载")
    WebDriverWait(driver, DEFAULT_WAIT_SECONDS).until(
        lambda current: "/plan/" in current.current_url or "/order/" in current.current_url
    )
    pause("订单页已加载")

    cancel_old_pending_order(driver)
    select_monthly_option(driver)
    apply_redeem_code(driver, redeem_code)
    zero_area = wait_for_zero_monthly(driver)
    print(f"已检测到月付金额：{zero_area.text.strip()}。")
    cancel_old_pending_order(driver)
    wait_for_zero_monthly(driver)
    if dry_run:
        print("dry-run：已确认月付为 0，未点击“立即订购”。")
        return None
    order_id = click_order_after_zero(driver)
    print("订单已创建并进入订单详情；零元订单会直接完成。")
    return order_id


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="52Pokemon 入门精灵球套餐助手")
    parser.add_argument(
        "--confirm-order",
        action="store_true",
        help="兼容旧命令；当前版本不再需要输入确认文本",
    )
    parser.add_argument(
        "--step-delay",
        type=float,
        default=STEP_DELAY_SECONDS,
        help="每个页面操作后的额外等待秒数，默认读取 POKEMON_STEP_DELAY",
    )
    parser.add_argument(
        "--redeem-code",
        default=os.environ.get("POKEMON_REDEEM_CODE", ""),
        help="兑换码；默认读取 POKEMON_REDEEM_CODE",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="确认月付为 0 后停止，不点击立即订购",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="不等待键盘输入，适合 GitHub Actions",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("POKEMON_BASE_URL", DEFAULT_BASE_URL),
        help="购买网站基础地址，默认 https://web1.52pokemon.cc",
    )
    return parser.parse_args(argv)


def run(
    redeem_code: str | None = None,
    dry_run: bool = False,
    step_delay: float | None = None,
    non_interactive: bool = False,
    base_url: str | None = None,
) -> str | None:
    """Run one purchase and return the detected order ID.

    A dry run stops after the zero-price check and never clicks the order button.
    """

    if base_url:
        set_base_url(base_url)
    code = (redeem_code or os.environ.get("POKEMON_REDEEM_CODE", "")).strip()
    if not code:
        raise PurchaseFailure(
            "没有提供兑换码", code="missing_redeem_code", retryable=False
        )
    non_interactive = non_interactive or os.environ.get(
        "POKEMON_NON_INTERACTIVE", "0"
    ).lower() in {"1", "true", "yes"}
    if step_delay is not None:
        global STEP_DELAY_SECONDS
        STEP_DELAY_SECONDS = max(0.0, step_delay)

    driver = setup_driver()
    try:
        try:
            login_if_needed(driver, non_interactive=non_interactive)
            order_id = prepare_order(driver, code, dry_run=dry_run)
            save_page_screenshot(
                driver,
                "dry-run-success" if dry_run else "purchase-success",
            )
            if not non_interactive:
                try:
                    input(
                        "浏览器已停在支付前页面，按 Enter 退出脚本"
                        "（不要在此处输入支付信息）："
                    )
                except EOFError:
                    print("非交互运行，脚本结束。")
            return order_id
        except BaseException:
            save_page_screenshot(driver, "purchase-failure")
            save_browser_diagnostics(driver)
            save_navigation_diagnostics(driver)
            raise
    except TimeoutException as exc:
        raise PurchaseFailure(
            f"页面等待超时，请检查登录状态或页面结构：{exc}",
            code="timeout",
            retryable=True,
        ) from exc
    except WebDriverException as exc:
        raise PurchaseFailure(
            f"浏览器或页面操作失败：{exc}",
            code="webdriver_error",
            retryable=True,
        ) from exc
    finally:
        driver.quit()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except AttributeError:
        pass
    global STEP_DELAY_SECONDS
    STEP_DELAY_SECONDS = max(0.0, args.step_delay)
    try:
        run(
            redeem_code=args.redeem_code,
            dry_run=args.dry_run,
            step_delay=args.step_delay,
            non_interactive=args.non_interactive,
            base_url=args.base_url,
        )
    except PurchaseFailure as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
