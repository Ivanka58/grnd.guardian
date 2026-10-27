import asyncio
import re
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

SITE_ROOT = "https://grnd.gg"
ADMINS_URL = f"{SITE_ROOT}/admin/admins"
COMPLAINTS_URL = f"{SITE_ROOT}/admin/complaints"

class SiteError(Exception):
    pass

class InvalidSession(SiteError):
    pass

class SiteStructureError(SiteError):
    pass

class GrndSession:
    def __init__(self, cookies: str):
        self.cookies = cookies.strip()
        self.timeout = aiohttp.ClientTimeout(total=25)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cookie": self.cookies,
            "Referer": "https://grnd.gg/",
            "Origin": "https://grnd.gg",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1"
        }

    async def fetch(self, url: str) -> tuple[str, str]:
        try:
            async with aiohttp.ClientSession(headers=self.headers, timeout=self.timeout) as session:
                async with session.get(url, allow_redirects=True) as response:
                    final_url = str(response.url)
                    html = await response.text(errors="replace")

                    if response.status in (401, 403):
                        raise InvalidSession("Сессия недействительна (401/403)")

                    if response.status >= 400:
                        raise SiteError(f"Сайт вернул HTTP {response.status}")

                    parsed = urlparse(final_url)
                    if parsed.hostname not in ("grnd.gg", "www.grnd.gg"):
                        raise InvalidSession("Перенаправление на посторонний адрес")

                    if re.search(r"/(?:login|signin|sign-in|auth)(?:/|$)", parsed.path, re.IGNORECASE):
                        raise InvalidSession("Сессия истекла или неверные куки (редирект на логин)")

                    return final_url, html
        except (InvalidSession, SiteError):
            raise
        except asyncio.TimeoutError as exc:
            raise SiteError("Время ожидания сайта истекло") from exc
        except aiohttp.ClientError as exc:
            raise SiteError("Ошибка HTTP-запроса к сайту") from exc
        except Exception as exc:
            raise SiteError("Не удалось загрузить страницу") from exc

    @staticmethod
    def parse_soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    async def profile(self, nickname: str | None = None) -> dict:
        try:
            final_url, html = await self.fetch(ADMINS_URL)
            soup = self.parse_soup(html)

            rows = soup.select("tr")
            candidates = []

            for row in rows:
                cells = row.find_all("td", recursive=False)
                if len(cells) < 8:
                    continue

                nickname_node = cells[4].select_one("span.table-cell")
                level_node = cells[7].select_one("button span.actions-button__label")

                if not nickname_node or not level_node:
                    continue

                current_nickname = nickname_node.get_text(" ", strip=True)
                level_text = level_node.get_text(" ", strip=True)
                match = re.fullmatch(r"Level\s+(\d+)", level_text, re.IGNORECASE)

                if not current_nickname or not match:
                    continue

                entry = {"nickname": current_nickname, "lvl": int(match.group(1))}
                candidates.append(entry)

                if nickname and current_nickname.casefold() == nickname.strip().casefold():
                    return entry

            if nickname:
                raise SiteStructureError(f"Администратор {nickname} не найден в таблице")

            if len(candidates) == 1:
                return candidates[0]

            if not candidates:
                raise SiteStructureError("Не удалось найти таблицу админов. Возможно, изменились куки или структура сайта.")

            raise SiteStructureError("Найдено несколько администраторов. Укажите никнейм.")

        except (InvalidSession, SiteError):
            raise
        except Exception as exc:
            raise SiteStructureError("Ошибка обработки таблицы администраторов") from exc

    async def complaints(self) -> list[dict]:
        try:
            final_url, html = await self.fetch(COMPLAINTS_URL)
            parsed_url = urlparse(final_url)

            if parsed_url.path.rstrip("/") != "/admin/complaints":
                raise InvalidSession("Не удалось открыть страницу жалоб")

            soup = self.parse_soup(html)
            blocks = soup.select("div.admin-complaints-content")

            if not blocks:
                raise SiteStructureError("Блоки жалоб не найдены на странице")

            result = []
            for block in blocks:
                try:
                    info = {}
                    for item in block.select("div.content-info > div.item"):
                        label_node = item.select_one("div.label")
                        title_node = item.select_one("div.title")

                        if not label_node or not title_node:
                            continue

                        label = label_node.get_text(" ", strip=True)
                        value = title_node.get_text(" ", strip=True)

                        if label == "Жалоба":
                            info["complaint_id"] = re.sub(r"^\s*#\s*", "", value).strip()
                        elif label == "Жалоба от":
                            info["sender"] = value
                        elif label == "Жалоба на":
                            info["target"] = value
                        elif label == "Дата подачи жалобы":
                            info["date"] = value

                    text_node = block.select_one("div.descriptions > div.flex > div > div.text")
                    evidence_nodes = block.select("div.resources > img[src]")
                    
                    evidence_urls = [image.get("src", "").strip() for image in evidence_nodes if image.get("src", "").strip()]
                    evidence_urls = [url for url in evidence_urls if urlparse(url).scheme in ("http", "https")]

                    complaint_id = info.get("complaint_id", "")
                    if not complaint_id or not complaint_id.isdigit():
                        continue

                    result.append({
                        "complaint_id": complaint_id,
                        "sender": info.get("sender", ""),
                        "target": info.get("target", ""),
                        "date": info.get("date", ""),
                        "text": (text_node.get_text(" ", strip=True) if text_node else ""),
                        "evidence_url": (evidence_urls[0] if evidence_urls else None),
                        "evidence_urls": evidence_urls,
                        "url": f"{COMPLAINTS_URL}/{complaint_id}",
                    })
                except Exception:
                    continue

            return result
        except (InvalidSession, SiteError):
            raise
        except Exception as exc:
            raise SiteStructureError("Ошибка обработки списка жалоб") from exc

async def check_session(cookies: str) -> dict:
    return await GrndSession(cookies).profile()

async def fetch_complaints(cookies: str) -> list[dict]:
    return await GrndSession(cookies).complaints()
