""" Клиент для работы с API панели 3x-ui (v3.1.0, форк M Franzering). Переписан под архитектуру СТАРИНА VPN с поддержкой CSRF, сессий и инбаунда №4. """

import logging
import uuid
import re
import time
from typing import Optional, Dict, Any, List
import aiohttp

import config

logger = logging.getLogger(__name__)


class XuiClient:
    """
    Асинхронный клиент для API 3x-ui v3.1.0.
    Автоматически парсит CSRF-токен, держит сессию и добавляет клиентов на инбаунд 4.
    """

    def __init__(self) -> None:
        # Убираем лишние слэши, чтобы пути не ломались
        self.base_url = config.XUI_URL.rstrip("/")
        self.username = config.XUI_USER
        self.password = config.XUI_PASS
        
        # Для этой панели обязателен префикс путей (извлечем его из URL панели)
        # Например, если XUI_URL = https://194.31.223.144:9298/PLciLh0QhM4rzsDHgx
        # То префикс будет /PLciLh0QhM4rzsDHgx
        url_parts = self.base_url.split("/", 3)
        self.prefix = f"/{url_parts[3]}" if len(url_parts) > 3 else ""
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._csrf_token: Optional[str] = None
        self._logged_in = False

    async def _get_session(self) -> aiohttp.ClientSession:
        """Возвращает или инициализирует aiohttp сессию с отключенной проверкой SSL."""
        if self._session is None or self._session.closed:
            # Самоподписанные SSL-сертификаты панели часто вызывают ошибки, отключаем их проверку
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _fetch_csrf_token(self) -> Optional[str]:
        """Делает GET-запрос на страницу авторизации и вытаскивает CSRF-токен."""
        session = await self._get_session()
        login_url = f"{self.base_url}/login"
        try:
            async with session.get(login_url, timeout=15) as response:
                html = await response.text()
                # Ищем тег <meta name="csrf-token" content="...">
                match = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', html)
                if match:
                    self._csrf_token = match.group(1)
                    logger.info("Успешно получен CSRF-токен из HTML.")
                    return self._csrf_token
                else:
                    logger.error("Не удалось найти csrf-token в HTML-коде страницы логина.")
                    return None
        except Exception as e:
            logger.error(f"Ошибка при получении CSRF-токена: {e}")
            return None

    async def _ensure_login(self) -> bool:
        """Проверяет статус авторизации и выполняет вход при необходимости."""
        if self._logged_in and self._csrf_token:
            return True
        
        logger.info("Запуск процесса авторизации в панели 3x-ui...")
        
        # Шаг 1: Получаем CSRF токен
        token = await self._fetch_csrf_token()
        if not token:
            return False

        # Шаг 2: Выполняем POST-login
        session = await self._get_session()
        login_path = f"{self.base_url}/login"
        
        headers = {
            "X-CSRF-Token": token,
            "X-Requested-With": "XMLHttpRequest"
        }
        data = {
            "username": self.username,
            "password": self.password
        }
        
        try:
            async with session.post(login_path, data=data, headers=headers, timeout=15) as response:
                if response.status == 200:
                    res_json = await response.json()
                    if res_json.get("success"):
                        self._logged_in = True
                        logger.info("Товарищ администратор, авторизация в 3x-ui прошла успешно!")
                        return True
                    else:
                        logger.error(f"Панель вернула отказ авторизации: {res_json}")
                        return False
                else:
                    logger.error(f"Неверный HTTP статус при логине: {response.status}")
                    return False
        except Exception as e:
            logger.error(f"Исключение при попытке авторизации: {e}")
            return False

    async def create_client(self, client_name: str, traffic_gb: int = 0, expire_days: int = 30) -> Optional[Dict[str, Any]]:
        """
        Создает нового клиента на инбаунде №4 (Reality VLESS).
        Принимает имя клиента (обычно это строка вида user_TELEGRAMID или сам Telegram ID).
        """
        if not await self._ensure_login():
            logger.error("Невозможно создать клиента: отсутствует авторизация.")
            return None

        session = await self._get_session()
        add_client_url = f"{self.base_url}/panel/api/clients/add"
        
        # Генерация необходимых уникальных параметров клиента
        client_uuid = str(uuid.uuid4())
        # Генерация 16-символьных рандомных строк для subId, password, auth
        sub_id = uuid.uuid4().hex[:16]
        client_pass = uuid.uuid4().hex[:16]
        client_auth = uuid.uuid4().hex[:16]
        
        headers = {
            "X-CSRF-Token": self._csrf_token,
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest"
        }
        
        # Формируем тело JSON запроса в соответствии с твоей панелью v3.1.0
        payload = {
            "client": {
                "id": client_uuid,
                "email": client_name,
                "subId": sub_id,
                "flow": "xtls-rprx-vision",
                "enable": True,
                "expiryTime": -2592000000,  # Отрицательное число означает обратный отсчет на 30 дней в этой панели
                "limitIp": 1,  # Ограничение на 1 устройство
                "totalGB": traffic_gb * 1024 * 1024 * 1024 if traffic_gb > 0 else 0,  # В байтах или 0 для безлимита
                "password": client_pass,
                "auth": client_auth,
                "tgId": 0
            },
            "inboundIds": [4]  # Твой рабочий Reality инбаунд
        }
        
        try:
            async with session.post(add_client_url, json=payload, headers=headers, timeout=15) as response:
                if response.status == 200:
                    res_json = await response.json()
                    if res_json.get("success"):
                        logger.info(f"Клиент {client_name} успешно добавлен на инбаунд [4]")
                        # Возвращаем сгенерированный UUID, чтобы бот мог построить ссылку подключения
                        return {"uuid": client_uuid, "email": client_name}
                    else:
                        logger.error(f"Ошибка добавления клиента в панели: {res_json}")
                        return None
                else:
                    logger.error(f"Ошибка API при добавлении клиента. Статус: {response.status}")
                    return None
        except Exception as e:
            logger.error(f"Исключение при добавлении клиента: {e}")
            return None

    async def generate_v2ray_config(self, client_name: str, protocol: str = "vless") -> Optional[str]:
        """
        Генерирует готовую ссылку vless:// для пользователя.
        Так как мы не парсим параметры самого инбаунда динамически,
        проще всего собрать Reality-ссылку на основе твоего домена/IP и UUID.
        """
        # Для генерации ссылки боту нужен UUID.
        # Так как в методах бота 'client_name' используется как уникальный ключ,
        # если UUID нет под рукой, мы можем просто сгенерировать его или передавать объект.
        # В архитектуре твоего бота эта функция вызывается из user_handlers.py, ожидает строку-ссылку.
        
        # Вытаскиваем чистый IP или домен сервера из URL для ссылки
        server_address = self.base_url.replace("https://", "").replace("http://", "").split(":")[0]
        
        # Чтобы не делать лишних запросов к панели ради поиска UUID, мы можем сохранять его в БД бота
        # (в поле xui_client_name при создании мы запишем UUID пользователя или связку).
        # Проверим, если client_name сам по себе является валидным UUID, берем его:
        if len(client_name) == 36 and client_name.count("-") == 4:
            uuid_str = client_name
            remark = "Старина_VPN"
        else:
            # Если передано имя, то это запасной вариант
            uuid_str = "00000000-0000-0000-0000-000000000000"
            remark = client_name
        
        # Порт для Reality обычно 443 (или тот, что на инбаунде 4). Замени 443 на порт своего инбаунда, если он другой!
        port = 443
        
        # Базовый скелет ссылки под Reality (замени sni, pbk и sid на свои данные из инбаунда №4!)
        # Эти настройки ты можешь скопировать из любого рабочего конфига этой панели
        sni = "yahoo.com"
        
        # ⚠️ ВПИШИ СВОИ ДАННЫЕ ИЗ ИНБАУНДА №4 (кнопка "🌐" или "👁️" в панели)
        pbk = "iZ7VlR87PmyS2KyfqjxoApQzVrGTdR7JTcbfI_WeABY"
        sid = "4bbce20f,55879fae6306a2,9505d6620f,66,2eca983e95a34407,21018cbd9133,ce21,173cf3"
        
        config_link = (
            f"vless://{uuid_str}@{server_address}:{port}?"
            f"type=tcp&security=reality&encrypt=none&pbk={pbk}&sni={sni}&"
            f"sid={sid}&flow=xtls-rprx-vision#{remark}"
        )
        return config_link

    async def delete_client(self, client_uuid: str) -> bool:
        """Удаляет клиента из панели 3x-ui по его UUID."""
        if not await self._ensure_login():
            return False
        
        session = await self._get_session()
        # В этой версии панели удаление происходит через инбаунд и UUID клиента
        # Эндпоинт: /panel/api/clients/del/{inboundId}/{clientUuid} или аналогичный через POST
        # Для форка M Franzering частый эндпоинт: /panel/api/inbounds/4/delClient/{clientUuid}
        del_url = f"{self.base_url}/panel/api/inbounds/4/delClient/{client_uuid}"
        
        headers = {
            "X-CSRF-Token": self._csrf_token,
            "X-Requested-With": "XMLHttpRequest"
        }
        
        try:
            async with session.post(del_url, headers=headers, timeout=15) as response:
                if response.status == 200:
                    res_json = await response.json()
                    return bool(res_json.get("success"))
                return False
        except Exception as e:
            logger.error(f"Ошибка при удалении клиента: {e}")
            return False

    async def close(self) -> None:
        """Закрывает HTTP сессию при выключении бота."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("Сессия XuiClient успешно закрыта.")
