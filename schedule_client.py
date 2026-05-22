from __future__ import annotations

import os

import requests

from rating_client import RatingClient


class ScheduleFetchError(RuntimeError):
    pass


class ScheduleClient:
    def __init__(
        self,
        *,
        login: str | None = None,
        password: str | None = None,
        cookie_header: str | None = None,
    ) -> None:
        self.url = os.getenv("REA_LESSONS_URL", "https://student.rea.ru/lessons/index.php?login=yes")
        self.login = (login if login is not None else os.getenv("REA_LOGIN", "")).strip()
        self.password = (password if password is not None else os.getenv("REA_PASSWORD", "")).strip()
        self.cookie_header = (cookie_header if cookie_header is not None else os.getenv("REA_COOKIE_HEADER", "")).strip()
        self.timeout = float(os.getenv("REA_SCHEDULE_REQUEST_TIMEOUT", os.getenv("REA_REQUEST_TIMEOUT", "15")))

    def fetch_week_html(self) -> str:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                )
            }
        )

        try:
            if self.cookie_header:
                response = session.get(
                    self.url,
                    headers={"Cookie": self.cookie_header},
                    timeout=self.timeout,
                )
                if self._is_authorized_lessons_page(response.text):
                    return response.text

            if not self.login or not self.password:
                raise ScheduleFetchError("Нет логина и пароля для получения расписания.")

            rating_client = RatingClient(login=self.login, password=self.password)
            rating_client._login(session)
            response = session.get(self.url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ScheduleFetchError("Сайт РЭУ временно не отвечает. Попробуйте позже.") from exc
        except Exception as exc:
            if isinstance(exc, ScheduleFetchError):
                raise
            raise ScheduleFetchError(str(exc)) from exc

        if not self._is_authorized_lessons_page(response.text):
            raise ScheduleFetchError("Авторизация прошла, но страница расписания не содержит данных.")

        return response.text

    def _is_authorized_lessons_page(self, html: str) -> bool:
        return "table_lessons" in html and "Расписание" in html
