from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Browser, Frame, Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


APP_VERSION = "1.3.4"


class OntAutomationError(RuntimeError):
    pass


MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "HG8245W5-6T": {
        "name": "HG8245W5-6T",
        "tr069_paths": ["/html/ssmp/tr069/tr069.asp"],
        "menu_sequence": ["Advanced", "System Management", "TR-069"],
    },
    "EG8141A5": {
        "name": "EG8141A5",
        "tr069_paths": ["/html/ssmp/tr069/tr069.asp"],
        "menu_sequence": ["Advanced", "System Management", "TR-069"],
    },
    "HG8245Q2": {
        "name": "HG8245Q2",
        "tr069_paths": [],
        "menu_sequence": ["System Tools", "TR-069"],
        "legacy_acs_flow": True,
    },
    "HG8546M": {
        "name": "HG8546M",
        "tr069_paths": [],
        "menu_sequence": ["System Tools", "TR-069"],
        "legacy_acs_flow": True,
    },
}

DEFAULT_PROFILE = {
    "name": "default",
    "tr069_paths": ["/html/ssmp/tr069/tr069.asp"],
    "menu_sequence": ["Advanced", "System Management", "System Tools", "TR-069"],
}


def print_results(results: list[dict[str, str]]) -> None:
    print("\nResumo da execucao:")
    print("-" * 88)
    print(f"{'ONT':<32} {'MODELO':<16} {'STATUS':<10} ERRO")
    print("-" * 88)
    for result in results:
        print(f"{result['target']:<32} {result.get('model', ''):<16} {result['status']:<10} {result.get('error', '')}")
    print("-" * 88)
    successes = sum(1 for result in results if result["status"] == "SUCESSO")
    failures = len(results) - successes
    print(f"Total: {len(results)} | Sucesso: {successes} | Erro: {failures}")


async def save_debug_artifacts(page: Page, reason: str) -> None:
    debug_dir = Path("debug")
    debug_dir.mkdir(exist_ok=True)
    safe_reason = "".join(char if char.isalnum() else "_" for char in reason).strip("_").lower()
    if not safe_reason:
        safe_reason = "debug"
    await page.screenshot(path=str(debug_dir / f"{safe_reason}.png"), full_page=True)
    (debug_dir / f"{safe_reason}.html").write_text(await page.content(), encoding="utf-8")


def get_root_page(root: Page | Frame) -> Page:
    return root.page if isinstance(root, Frame) else root


def expand_ip_range(value: str) -> list[str]:
    start_text, end_text = [part.strip() for part in value.split("-", 1)]
    start_ip = ipaddress.ip_address(start_text)
    if "." in end_text:
        end_ip = ipaddress.ip_address(end_text)
    else:
        if start_ip.version != 4:
            raise OntAutomationError(f"Range abreviado so e suportado para IPv4: {value}")
        octets = start_text.split(".")
        octets[-1] = end_text
        end_ip = ipaddress.ip_address(".".join(octets))

    if start_ip.version != end_ip.version or int(end_ip) < int(start_ip):
        raise OntAutomationError(f"Range de IP invalido: {value}")
    return [str(ipaddress.ip_address(ip)) for ip in range(int(start_ip), int(end_ip) + 1)]


def expand_targets(ont: dict[str, Any]) -> list[str]:
    raw_targets = ont.get("targets")
    if not raw_targets:
        return [str(ont.get("url") or ont.get("ip")).strip()]
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]

    targets: list[str] = []
    for item in raw_targets:
        value = str(item).strip()
        if not value:
            continue
        if "://" in value:
            targets.append(value)
        elif "/" in value:
            network = ipaddress.ip_network(value, strict=False)
            targets.extend(str(ip) for ip in network.hosts())
        elif "-" in value:
            targets.extend(expand_ip_range(value))
        else:
            ipaddress.ip_address(value)
            targets.append(value)

    return list(dict.fromkeys(targets))


def build_ont_url(ont: dict[str, Any], target: str) -> str:
    target = target.strip().rstrip("/")
    if "://" in target:
        url = target
    else:
        protocol = str(ont.get("protocol", "https")).strip().rstrip(":/")
        port = ont.get("port")
        if port is None and ont.get("url"):
            parsed_default = urlparse(str(ont["url"]))
            port = parsed_default.port
            if parsed_default.scheme:
                protocol = parsed_default.scheme
        url = f"{protocol}://{target}"
        if port:
            url = f"{url}:{port}"

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise OntAutomationError("URL da ONT invalida. Exemplo: https://10.100.207.202:80")
    return f"{url}/index.asp" if parsed.path in {"", "/"} else url


def alternate_protocol_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return url.replace("https://", "http://", 1)
    if parsed.scheme == "http":
        return url.replace("http://", "https://", 1)
    return None


async def goto_ont_page(page: Page, url: str, timeout: int = 15000) -> None:
    await page.goto(url, wait_until="commit", timeout=timeout)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightTimeoutError:
        pass


def hg8245q2_informing_time(tr069: dict[str, Any]) -> str:
    value = str(tr069.get("informing_time") or "").strip()
    if not value or value == "0001-01-01T00:00:00Z":
        return "2009-12-20T12:23:34"
    return value


def uses_legacy_acs_flow(profile: dict[str, Any]) -> bool:
    return bool(profile.get("legacy_acs_flow"))


def target_label(target: str) -> str:
    parsed = urlparse(target)
    return parsed.netloc or target


async def detect_ont_model(page: Page) -> str:
    candidates: list[str] = []
    try:
        candidates.append(await page.title())
    except Exception:
        pass

    for selector in ["#headerProductName", "#headerProductNameOrg", "#headerProductNamePar", "body"]:
        try:
            text = await page.locator(selector).first.text_content(timeout=1200)
            if text:
                candidates.append(text)
        except Exception:
            continue

    joined = " ".join(candidates).upper()
    for model in MODEL_PROFILES:
        if model.upper() in joined:
            return model

    return "UNKNOWN"


def profile_for_model(model: str, browser_cfg: dict[str, Any]) -> dict[str, Any]:
    profile = deepcopy_profile(MODEL_PROFILES.get(model, DEFAULT_PROFILE))
    custom_paths = browser_cfg.get("tr069_paths")
    if isinstance(custom_paths, list) and custom_paths:
        profile["tr069_paths"] = custom_paths
    elif browser_cfg.get("tr069_path") and not uses_legacy_acs_flow(profile):
        profile["tr069_paths"] = [browser_cfg["tr069_path"]]
    return profile


def deepcopy_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": profile.get("name", "default"),
        "tr069_paths": list(profile.get("tr069_paths", [])),
        "menu_sequence": list(profile.get("menu_sequence", [])),
        "legacy_acs_flow": bool(profile.get("legacy_acs_flow", False)),
    }


async def proceed_through_privacy_warning(page: Page) -> None:
    advanced_selectors = [
        "#details-button",
        "button:has-text('Avancadas')",
        "button:has-text('Avançadas')",
        "button:has-text('Advanced')",
    ]
    proceed_selectors = [
        "#proceed-link",
        "a:has-text('Prosseguir')",
        "a:has-text('Proceed')",
        "text=/Prosseguir para/i",
        "text=/Proceed to/i",
    ]

    warning_visible = False
    for selector in ["#main-message", "text=/ligacao nao e privada/i", "text=/ligação não é privada/i", "text=/connection is not private/i"]:
        try:
            await page.locator(selector).first.wait_for(timeout=1200)
            warning_visible = True
            break
        except PlaywrightTimeoutError:
            continue

    if not warning_visible:
        return

    for selector in advanced_selectors:
        try:
            await page.locator(selector).first.click(timeout=2000)
            break
        except Exception:
            continue

    for selector in proceed_selectors:
        try:
            await page.locator(selector).first.click(timeout=3000)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            return
        except Exception:
            continue

    raise OntAutomationError("Apareceu o aviso de certificado, mas nao consegui clicar em Prosseguir.")


async def click_first_visible(page: Page, labels: list[str], timeout: int = 2500) -> None:
    for label in labels:
        candidates = [
            page.get_by_text(label, exact=True),
            page.get_by_role("link", name=label),
            page.get_by_role("button", name=label),
        ]
        for candidate in candidates:
            try:
                await candidate.first.click(timeout=timeout)
                return
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
    raise OntAutomationError(f"Nao encontrei nenhum item clicavel: {', '.join(labels)}")


async def visible_locator(root: Page | Frame, selectors: list[str], timeout: int = 800) -> Locator | None:
    for selector in selectors:
        locator = root.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            return locator
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return None


async def locate_login_fields(page: Page, user_selectors: list[str], pass_selectors: list[str]) -> tuple[Page | Frame | None, Locator | None, Locator | None]:
    fast_user_selectors = [
        "#txt_Username",
        "input[name='txt_Username']",
        "#Username",
        "#username",
        "#UserName",
        "input[name='Username']",
        "input[name='username']",
    ]
    fast_pass_selectors = [
        "#txt_Password",
        "input[name='txt_Password']",
        "#Password",
        "#password",
        "input[name='Password']",
        "input[name='password']",
        "input[type='password']",
    ]

    for root in [page, *page.frames]:
        user_input = await visible_locator(root, fast_user_selectors, timeout=180)
        pass_input = await visible_locator(root, fast_pass_selectors, timeout=180)
        if user_input is not None and pass_input is not None:
            return root, user_input, pass_input

    for root in [page, *page.frames]:
        user_input = await visible_locator(root, user_selectors, timeout=350)
        pass_input = await visible_locator(root, pass_selectors, timeout=350)
        if user_input is not None and pass_input is not None:
            return root, user_input, pass_input

    return None, None, None


async def input_after_label(root: Page | Frame, label_text: str) -> Locator:
    xpath = (
        "xpath=//*[normalize-space(.)="
        f"{json.dumps(label_text)}"
        f" or contains(normalize-space(.), {json.dumps(label_text)})]/following::input[1]"
    )
    locator = root.locator(xpath).first
    await locator.wait_for(state="attached", timeout=4000)
    return locator


async def set_input_value(locator: Locator, value: Any) -> None:
    value = "" if value is None else str(value)
    input_type = (await locator.get_attribute("type") or "").lower()
    if input_type in {"checkbox", "radio"}:
        if bool(value):
            if not await locator.is_checked():
                await locator.click(force=True)
        else:
            if await locator.is_checked():
                await locator.click(force=True)
        await locator.evaluate(
            """el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                if (typeof el.onclick === 'function') el.onclick();
            }"""
        )
        return

    await locator.fill(value)
    await locator.evaluate(
        """(el, value) => {
            el.value = value;
            el.setAttribute('value', value);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            if (typeof el.onchange === 'function') el.onchange();
            if (typeof el.onblur === 'function') el.onblur();
        }""",
        value,
    )


async def read_input_value(locator: Locator) -> str:
    input_type = (await locator.get_attribute("type") or "").lower()
    if input_type in {"checkbox", "radio"}:
        return "true" if await locator.is_checked() else "false"
    return await locator.input_value()


async def fill_by_label(root: Page | Frame, label_text: str, value: Any, verify: bool = True) -> None:
    try:
        target = await input_after_label(root, label_text)
    except PlaywrightTimeoutError as exc:
        raise OntAutomationError(f"Campo nao encontrado: {label_text}") from exc

    await set_input_value(target, value)
    if verify and (await target.get_attribute("type") or "").lower() not in {"password"}:
        actual = await read_input_value(target)
        expected = "" if value is None else str(value)
        if actual != expected and expected.lower() not in {"true", "false"}:
            raise OntAutomationError(f"Campo {label_text} nao foi preenchido. Esperado {expected}, ficou {actual}.")


async def fill_hg8245q2_acs(root: Page | Frame, tr069: dict[str, Any]) -> None:
    current_interval = await read_input_value(await input_after_label(root, "Informing Interval:"))
    await fill_by_label(root, "Enable ACS Management:", True)
    await fill_by_label(root, "Enable Periodic Informing:", True)
    await fill_by_label(root, "Informing Interval:", current_interval)
    await fill_by_label(root, "Informing Time:", hg8245q2_informing_time(tr069))
    await fill_by_label(root, "ACS URL:", tr069["acs_url"])
    await fill_by_label(root, "ACS User Name:", tr069.get("acs_username", ""))
    await fill_by_label(root, "ACS Password:", tr069.get("acs_password", ""), verify=False)
    await fill_by_label(root, "Connection Request User Name:", tr069.get("connection_request_username", ""))
    await fill_by_label(root, "Connection Request Password:", tr069.get("connection_request_password", ""), verify=False)
    await fill_by_label(root, "DSCP:", tr069.get("dscp", 0))


def expected_informing_time(profile: dict[str, Any], tr069: dict[str, Any]) -> str:
    if uses_legacy_acs_flow(profile):
        return hg8245q2_informing_time(tr069)
    return str(tr069.get("informing_time", "0001-01-01T00:00:00Z"))


async def verify_acs_persisted(root: Page | Frame, tr069: dict[str, Any], profile: dict[str, Any]) -> None:
    expected_values = {
        "Enable ACS Management:": "true",
        "Enable Periodic Informing:": "true",
        "Informing Time:": expected_informing_time(profile, tr069),
        "ACS URL:": str(tr069["acs_url"]),
        "ACS User Name:": str(tr069.get("acs_username", "")),
        "Connection Request User Name:": str(tr069.get("connection_request_username", "")),
        "DSCP:": str(tr069.get("dscp", 0)),
    }
    if not uses_legacy_acs_flow(profile):
        expected_values["Informing Interval:"] = str(tr069.get("informing_interval", 43200))

    mismatches: list[str] = []
    for name, expected in expected_values.items():
        actual = await read_input_value(await input_after_label(root, name))
        if actual != expected:
            mismatches.append(f"{name}: esperado {expected}, ficou {actual}")

    if mismatches:
        raise OntAutomationError(
            f"{profile.get('name')} nao salvou a configuracao TR-069: " + "; ".join(mismatches)
        )


async def click_apply_button(root: Page | Frame, profile: dict[str, Any]) -> None:
    if uses_legacy_acs_flow(profile):
        apply_after_dscp = root.locator(
            "xpath=//*[contains(normalize-space(.), 'DSCP:')]/following::input[@value='Apply' or @type='submit'][1]"
        ).first
        try:
            page = get_root_page(root)
            await apply_after_dscp.click(timeout=4000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(3000)
            await page.wait_for_timeout(7000)
            return
        except Exception as exc:
            raise OntAutomationError(f"{profile.get('name')}: nao consegui clicar no Apply apos DSCP: {exc}") from exc
    else:
        apply_selectors = [
            "#ACSbtnApply",
            "input[id='ACSbtnApply']",
            "input[onclick*='SubmitAcsConfig']",
            "input[value='Apply'][onclick*='Acs']",
            "input[value='Apply'][onclick*='ACS']",
            "xpath=//*[contains(normalize-space(.), 'ACS Parameter Settings')]/following::input[@value='Apply'][1]",
            "xpath=//*[contains(normalize-space(.), 'ACS Parameter Settings')]/following::button[contains(normalize-space(.), 'Apply')][1]",
        ]

    for selector in apply_selectors:
        button = root.locator(selector)
        try:
            if await button.count() == 1:
                page = get_root_page(root)
                try:
                    await button.click(timeout=4000)
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeoutError:
                    await page.wait_for_timeout(3000)
                return
        except Exception:
            continue

    buttons = root.locator("input[value='Apply'], button:has-text('Apply')")
    count = await buttons.count()
    if count == 1:
        page = get_root_page(root)
        await buttons.click(timeout=4000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            await page.wait_for_timeout(3000)
        return

    raise OntAutomationError(
        f"Encontrei {count} botoes Apply e nao consegui identificar o Apply correto do perfil {profile.get('name')}."
    )


async def try_login(page: Page, username: str, password: str) -> None:
    await proceed_through_privacy_warning(page)

    user_selectors = [
        "input[name='txt_Username']",
        "input[id='txt_Username']",
        "input[name='User']",
        "input[id='User']",
        "input[name='username']",
        "input[name='Username']",
        "input[name='UserName']",
        "input[id='username']",
        "input[id='Username']",
        "input[id='UserName']",
        "input[placeholder*='user' i]",
        "input[placeholder*='usuario' i]",
        "input[placeholder*='usuário' i]",
        "input[id*='user' i]",
        "input[name*='user' i]",
        "input[type='text']:visible",
    ]
    pass_selectors = [
        "input[name='txt_Password']",
        "input[id='txt_Password']",
        "input[name='Pass']",
        "input[id='Pass']",
        "input[name='password']",
        "input[name='Password']",
        "input[id='password']",
        "input[id='Password']",
        "input[placeholder*='pass' i]",
        "input[placeholder*='senha' i]",
        "input[id*='pass' i]",
        "input[name*='pass' i]",
        "input[type='password']:visible",
    ]

    login_root, user_input, pass_input = await locate_login_fields(page, user_selectors, pass_selectors)

    if login_root is None or user_input is None or pass_input is None:
        if await page.locator("text=Home Page").count() or await page.locator("text=Network connection status").count():
            print("Login nao necessario: a ONT ja parece estar autenticada.")
            return
        raise OntAutomationError(
            "Nao encontrei os campos de usuario/senha."
        )

    print("Tela de login encontrada. Preenchendo usuario e senha...")
    await user_input.fill(username)
    await pass_input.fill(password)

    async def submit_login_form() -> None:
        try:
            await page.keyboard.press("Enter")
            return
        except Exception:
            pass
        try:
            await page.evaluate(
                """() => {
                    const password = document.querySelector('input[type="password"]');
                    const form = password && password.form;
                    if (form) {
                        if (typeof form.submit === 'function') form.submit();
                        else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                    }
                }"""
            )
        except Exception:
            pass

    login_selectors = [
        "#loginbutton",
        "#loginBtn",
        "#btnLogin",
        "#btn_login",
        "#button",
        "#Button",
        "#btnSubmit",
        "input[name='Login']",
        "input[name='Submit']",
        "input[id*='login' i]",
        "button[id*='login' i]",
        "input[value='Login']",
        "input[value='Log In']",
        "input[value='Entrar']",
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Login')",
        "button:has-text('Log In')",
        "button:has-text('Entrar')",
        "a:has-text('Login')",
        "a:has-text('Entrar')",
    ]
    login_button = await visible_locator(login_root, login_selectors, timeout=1000)
    if login_button is not None:
        try:
            await login_button.click(timeout=3000)
        except Exception:
            await submit_login_form()
    else:
        generic_buttons = login_root.locator("input[type='button']:visible, input[type='submit']:visible, button:visible")
        if await generic_buttons.count() == 1:
            await generic_buttons.nth(0).click(timeout=3000)
        else:
            await submit_login_form()

    try:
        await page.wait_for_function(
            """() => !document.querySelector('input[type="password"]') ||
                   document.body.innerText.includes('Logout') ||
                   document.body.innerText.includes('Home Page') ||
                   document.body.innerText.includes('System Tools')""",
            timeout=5000,
        )
    except PlaywrightTimeoutError:
        pass

    if await page.locator("input[type='password']:visible").count() > 0:
        retry_button = page.locator("input[type='button']:visible, input[type='submit']:visible, button:visible").first
        try:
            await retry_button.click(timeout=2000)
            await page.wait_for_timeout(2500)
        except Exception:
            pass

    if await page.locator("input[type='password']:visible").count() > 0:
        raise OntAutomationError(
            "A senha foi enviada, mas a tela de login continuou aberta. Confira usuario/senha."
        )

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=3000)
    except PlaywrightTimeoutError:
        return


async def find_tr069_root(page: Page) -> Page | Frame:
    roots: list[Page | Frame] = [page, *page.frames]
    for root in roots:
        try:
            if await root.locator("text=ACS Configuration").count() > 0:
                return root
            if await root.locator("text=ACS Parameter Settings").count() > 0:
                return root
        except Exception:
            continue
    raise OntAutomationError("Nao encontrei a tela ACS Configuration/TR-069.")


async def navigate_to_tr069(page: Page, browser_cfg: dict[str, Any], profile: dict[str, Any]) -> Page | Frame:
    base_url = page.url.split("/index.asp")[0].split("/html/")[0].rstrip("/")
    for raw_path in profile.get("tr069_paths", []):
        tr069_path = str(raw_path)
        if not tr069_path.startswith("/"):
            tr069_path = f"/{tr069_path}"
        try:
            print(f"Abrindo TR-069 diretamente em {tr069_path}...")
            await goto_ont_page(page, f"{base_url}{tr069_path}", timeout=8000)
            await proceed_through_privacy_warning(page)
            return await find_tr069_root(page)
        except Exception:
            print("Caminho direto nao abriu a tela TR-069. Tentando proxima opcao...")

    await goto_ont_page(page, f"{base_url}/index.asp", timeout=10000)
    await proceed_through_privacy_warning(page)
    for selector in ["#addconfig", "#systool", "#tr069config"]:
        try:
            item = page.locator(selector)
            if await item.count() == 1:
                await item.click(timeout=2500)
        except Exception:
            continue
    for menu_label in profile.get("menu_sequence", []):
        try:
            await click_first_visible(page, [menu_label], timeout=1600)
            await page.wait_for_timeout(300)
        except OntAutomationError:
            pass
    await page.wait_for_load_state("networkidle", timeout=10000)
    return await find_tr069_root(page)


async def pause_for_inspection(message: str) -> None:
    print(message)
    await asyncio.to_thread(input, "Pressione Enter para continuar...")


async def apply_tr069_settings(
    root: Page | Frame,
    tr069: dict[str, Any],
    dry_run: bool,
    save_success_debug: bool,
    profile: dict[str, Any],
    inspect: bool = False,
) -> None:
    if uses_legacy_acs_flow(profile):
        await fill_hg8245q2_acs(root, tr069)
    else:
        await fill_by_label(root, "Enable ACS Management:", True)
        await fill_by_label(root, "Enable Periodic Informing:", True)
        await fill_by_label(root, "Informing Interval:", tr069.get("informing_interval", 43200))
        await fill_by_label(root, "Informing Time:", tr069.get("informing_time", "0001-01-01T00:00:00Z"))
        await fill_by_label(root, "ACS URL:", tr069["acs_url"])
        await fill_by_label(root, "ACS User Name:", tr069.get("acs_username", ""))
        await fill_by_label(root, "ACS Password:", tr069.get("acs_password", ""), verify=False)
        await fill_by_label(root, "Connection Request User Name:", tr069.get("connection_request_username", ""))
        await fill_by_label(root, "Connection Request Password:", tr069.get("connection_request_password", ""), verify=False)
        await fill_by_label(root, "DSCP:", tr069.get("dscp", 0))
    if save_success_debug:
        await save_debug_artifacts(get_root_page(root), "before_apply_tr069")

    if inspect:
        await pause_for_inspection(
            "Modo inspecao: confira no Chromium se os campos TR-069 foram preenchidos corretamente."
        )

    if dry_run:
        print("Dry-run ativo: campos preenchidos, mas Apply nao foi clicado.")
        return

    print("Campos TR-069 preenchidos. Clicando no Apply da configuracao ACS...")
    await click_apply_button(root, profile)


async def verify_after_apply(page: Page, browser_cfg: dict[str, Any], profile: dict[str, Any], tr069: dict[str, Any]) -> None:
    wait_key = "hg8245q2_save_wait_ms" if uses_legacy_acs_flow(profile) else "post_apply_wait_ms"
    default_wait = 10000 if uses_legacy_acs_flow(profile) else 5000
    await page.wait_for_timeout(int(browser_cfg.get(wait_key, default_wait)))
    verified_root = await navigate_to_tr069(page, browser_cfg, profile)
    await verify_acs_persisted(verified_root, tr069, profile)


async def logout_ont(page: Page) -> None:
    print("Deslogando da ONT...")
    logout_selectors = [
        "text=Logout",
        "text=Log out",
        "text=Sair",
        "#logout",
        "a:has-text('Logout')",
        "a:has-text('Sair')",
        "button:has-text('Logout')",
        "button:has-text('Sair')",
    ]
    for selector in logout_selectors:
        try:
            locator = page.locator(selector).first
            await locator.click(timeout=1500)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            return
        except Exception:
            continue

    try:
        base_url = page.url.split("/index.asp")[0].split("/html/")[0].rstrip("/")
        await goto_ont_page(page, f"{base_url}/logout.cgi?RequestFile=html/logout.html", timeout=5000)
    except Exception as exc:
        print(f"Aviso: nao consegui confirmar logout da ONT: {exc}")


async def process_target(
    context: Any,
    config: dict[str, Any],
    target: str,
    headed: bool,
    dry_run: bool,
    inspect: bool = False,
) -> dict[str, str]:
    ont = config["ont"]
    browser_cfg = config.get("browser", {})
    timeout_ms = int(browser_cfg.get("timeout_ms", 30000))
    ont_url = build_ont_url(ont, target)
    label = target_label(target)
    page: Page | None = None

    try:
        print(f"\n[{label}] Iniciando configuracao...")
        page = await context.new_page()
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
        page.set_default_timeout(timeout_ms)

        try:
            await goto_ont_page(page, ont_url, timeout=int(browser_cfg.get("initial_goto_timeout_ms", 30000)))
        except Exception as exc:
            fallback_url = alternate_protocol_url(ont_url)
            if fallback_url and ("ERR_CONNECTION_RESET" in str(exc) or "ERR_SSL" in str(exc) or "ERR_EMPTY_RESPONSE" in str(exc)):
                print(f"[{label}] Falha em {ont_url}. Tentando {fallback_url}...")
                ont_url = fallback_url
                await goto_ont_page(page, ont_url, timeout=int(browser_cfg.get("initial_goto_timeout_ms", 30000)))
            else:
                raise
        await proceed_through_privacy_warning(page)
        await try_login(page, ont["username"], ont["password"])
        model = await detect_ont_model(page)
        profile = profile_for_model(model, browser_cfg)
        print(f"[{label}] Modelo detectado: {model}. Perfil: {profile['name']}")
        tr069_root = await navigate_to_tr069(page, browser_cfg, profile)
        await apply_tr069_settings(
            tr069_root,
            config["tr069"],
            dry_run=dry_run,
            save_success_debug=bool(browser_cfg.get("save_success_debug", False)),
            profile=profile,
            inspect=inspect,
        )
        if not dry_run:
            await verify_after_apply(page, browser_cfg, profile, config["tr069"])
            if uses_legacy_acs_flow(profile):
                print(f"{profile.get('name')}: configuracao confirmada. Mantendo sessao aberta por alguns segundos antes do logout.")
                await page.wait_for_timeout(int(browser_cfg.get("hg8245q2_logout_wait_ms", 5000)))
            else:
                await page.wait_for_timeout(int(browser_cfg.get("post_verify_wait_ms", 1000)))
            await logout_ont(page)

        if headed:
            await page.wait_for_timeout(int(browser_cfg.get("headed_finish_wait_ms", 500)))
        await page.close()
        return {"target": label, "model": model, "profile": profile["name"], "status": "SUCESSO", "error": ""}
    except Exception as exc:
        if page is not None:
            try:
                await save_debug_artifacts(page, f"{label}_error")
                await page.close()
            except Exception:
                pass
        return {"target": label, "model": locals().get("model", ""), "profile": locals().get("profile", {}).get("name", ""), "status": "ERRO", "error": str(exc)}


async def run(config: dict[str, Any], headed: bool, dry_run: bool, inspect: bool = False) -> None:
    ont = config["ont"]
    browser_cfg = config.get("browser", {})
    targets = expand_targets(ont)
    results: list[dict[str, str]] = []

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=not headed,
            args=["--ignore-certificate-errors"],
        )
        context = await browser.new_context(
            ignore_https_errors=bool(browser_cfg.get("ignore_https_errors", True))
        )

        for target in targets:
            results.append(
                await process_target(
                    context,
                    config,
                    target,
                    headed=headed,
                    dry_run=dry_run,
                    inspect=inspect,
                )
            )
        await browser.close()

    print_results(results)
    if any(result["status"] == "ERRO" for result in results):
        raise SystemExit(1)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise OntAutomationError(f"Arquivo de configuracao nao existe: {path}")
    try:
        with path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except json.JSONDecodeError as exc:
        raise OntAutomationError(
            f"JSON invalido em {path}: linha {exc.lineno}, coluna {exc.colno}. "
            "Confira chaves, aspas, virgulas e se o arquivo comeca com { e termina com }."
        ) from exc

    for key in ("ont", "tr069"):
        if key not in config:
            raise OntAutomationError(f"Config sem bloco obrigatorio: {key}")
    if not config["ont"].get("targets") and not config["ont"].get("url") and not config["ont"].get("ip"):
        raise OntAutomationError("Config precisa de ont.targets, ont.url ou ont.ip.")
    for key in ("username", "password"):
        if not config["ont"].get(key):
            raise OntAutomationError(f"Config ont.{key} esta vazio.")
    if not config["tr069"].get("acs_url"):
        raise OntAutomationError("Config tr069.acs_url esta vazio.")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="Configura TR-069/ACS em ONT Huawei pela interface web.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    parser.add_argument("--config", default="config.json", help="Caminho do arquivo JSON de configuracao.")
    parser.add_argument("--headed", action="store_true", help="Mostra o navegador durante a automacao.")
    parser.add_argument("--dry-run", action="store_true", help="Preenche os campos, mas nao clica em Apply.")
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Pausa antes do Apply para inspecionar a tela preenchida no Chromium.",
    )
    args = parser.parse_args()

    try:
        config = load_config(Path(args.config))
        asyncio.run(run(config, headed=True if args.inspect else args.headed, dry_run=args.dry_run, inspect=args.inspect))
        print("Configuracao TR-069 concluida.")
    except Exception as exc:
        raise SystemExit(f"Erro: {exc}") from exc


if __name__ == "__main__":
    main()
