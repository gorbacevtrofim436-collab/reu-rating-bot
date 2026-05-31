from __future__ import annotations

import os
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class RatingFetchError(RuntimeError):
    pass


class ReaCaptchaRequired(RatingFetchError):
    pass


class InvalidCredentialsError(RatingFetchError):
    pass


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def create_rea_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=_env_int("REA_HTTP_RETRIES", 1),
        connect=_env_int("REA_HTTP_CONNECT_RETRIES", 1),
        read=_env_int("REA_HTTP_READ_RETRIES", 1),
        status=_env_int("REA_HTTP_STATUS_RETRIES", 1),
        backoff_factor=_env_float("REA_HTTP_BACKOFF_FACTOR", 0.8),
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


class RatingClient:
    def __init__(
        self,
        *,
        login: str | None = None,
        password: str | None = None,
        cookie_header: str | None = None,
    ) -> None:
        self.url = os.getenv(
            "REA_RATING_URL",
            "https://student.rea.ru/rating/index.php?login=yes&semester=2-й+семестр",
        )
        self.login_url = os.getenv("REA_LOGIN_URL", "https://student.rea.ru/")
        has_explicit_credentials = login is not None or password is not None
        self.login = (login if login is not None else os.getenv("REA_LOGIN", "")).strip()
        self.password = (password if password is not None else os.getenv("REA_PASSWORD", "")).strip()
        self.cookie_header = (
            cookie_header
            if cookie_header is not None
            else "" if has_explicit_credentials else os.getenv("REA_COOKIE_HEADER", "")
        ).strip()
        self.timeout = float(os.getenv("REA_REQUEST_TIMEOUT", "10"))

    def fetch_html(self) -> str:
        session = create_rea_session()

        try:
            if self.cookie_header:
                response = session.get(
                    self.url,
                    headers={"Cookie": self.cookie_header},
                    timeout=self.timeout,
                )
                if self._is_authorized_rating_page(response.text):
                    return response.text

            if not self.login or not self.password:
                raise RatingFetchError(
                    "REA_COOKIE_HEADER недействителен или не задан, а REA_LOGIN/REA_PASSWORD отсутствуют."
                )

            self._login(session)
            return self.fetch_html_from_session(session)
        except requests.RequestException as exc:
            raise RatingFetchError("Сайт РЭУ временно не отвечает. Попробуйте позже.") from exc

    def fetch_html_from_session(self, session: requests.Session) -> str:
        try:
            response = session.get(self.url, timeout=self.timeout)
            self._raise_for_status(response)
        except requests.RequestException as exc:
            raise RatingFetchError("Сайт РЭУ временно не отвечает. Попробуйте позже.") from exc

        if not self._is_authorized_rating_page(response.text):
            raise RatingFetchError("Авторизация прошла, но страница рейтинга не содержит данных.")

        return response.text

    def _login(self, session: requests.Session) -> None:
        try:
            response = session.get(self.login_url, timeout=self.timeout)
            self._raise_for_status(response)

            if self._looks_like_captcha_page(response.text):
                raise ReaCaptchaRequired(
                    "Сайт РЭУ временно включил антибот-проверку/CAPTCHA. "
                    "Бот не может обходить CAPTCHA. Попробуйте позже или откройте ЛКС вручную."
                )

            soup = BeautifulSoup(response.text, "html.parser")
            form = self._find_login_form(soup)
            if form is None:
                raise RatingFetchError(
                    "Не найдена форма входа USER_LOGIN/USER_PASSWORD на сайте РЭУ. "
                    "Сайт мог изменить авторизацию или показать защитную страницу."
                )

            post_url = urljoin(response.url, form.get("action") or "/index.php?login=yes")
            data: dict[str, str] = {}
            for input_node in form.find_all("input"):
                name = input_node.get("name")
                if not name:
                    continue
                data[name] = input_node.get("value") or ""

            data["USER_LOGIN"] = self.login
            data["USER_PASSWORD"] = self.password
            data.setdefault("Login", "Войти")

            login_response = session.post(
                post_url,
                data=data,
                timeout=self.timeout,
                allow_redirects=True,
            )
            self._raise_for_status(login_response)
        except requests.RequestException as exc:
            raise RatingFetchError("Сайт РЭУ временно не отвечает. Попробуйте позже.") from exc

        if self._looks_like_login_page(login_response.text):
            raise InvalidCredentialsError("Сайт не принял логин или пароль.")

    def _find_login_form(self, soup: BeautifulSoup):
        for form in soup.find_all("form"):
            input_names = {
                str(input_node.get("name") or "")
                for input_node in form.find_all("input")
            }
            if {"USER_LOGIN", "USER_PASSWORD"}.issubset(input_names):
                return form
        return None

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.status_code == 401 or response.status_code == 403:
            raise RatingFetchError("Сессия недействительна или доступ запрещен.")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RatingFetchError(f"Ошибка сайта рейтинга: HTTP {response.status_code}") from exc

    def _looks_like_login_page(self, html: str) -> bool:
        return "USER_LOGIN" in html and "USER_PASSWORD" in html

    def _is_authorized_rating_page(self, html: str) -> bool:
        return ".es-rating__line-parent" in html or "es-rating__line-parent" in html

    def _looks_like_captcha_page(self, html: str) -> bool:
        return (
            'id="captchaForm"' in html
            or 'name="pin"' in html
            or "/json/check" in html
            or "/captcha" in html
        )
