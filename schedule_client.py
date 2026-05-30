from __future__ import annotations

import os

import requests

from rating_client import RatingClient, create_rea_session


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
        has_explicit_credentials = login is not None or password is not None
        self.login = (login if login is not None else os.getenv("REA_LOGIN", "")).strip()
        self.password = (password if password is not None else os.getenv("REA_PASSWORD", "")).strip()
        self.cookie_header = (
            cookie_header
            if cookie_header is not None
            else "" if has_explicit_credentials else os.getenv("REA_COOKIE_HEADER", "")
        ).strip()
        self.timeout = float(os.getenv("REA_SCHEDULE_REQUEST_TIMEOUT", os.getenv("REA_REQUEST_TIMEOUT", "10")))

    def fetch_week_html(self) -> str:
        session = create_rea_session()

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
            return self.fetch_week_html_from_session(session)
        except requests.RequestException as exc:
            raise ScheduleFetchError("Сайт РЭУ временно не отвечает. Попробуйте позже.") from exc
        except Exception as exc:
            if isinstance(exc, ScheduleFetchError):
                raise
            raise ScheduleFetchError(str(exc)) from exc

    def fetch_week_html_from_session(self, session: requests.Session) -> str:
        try:
            response = session.get(self.url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ScheduleFetchError("Сайт РЭУ временно не отвечает. Попробуйте позже.") from exc

        if not self._is_authorized_lessons_page(response.text):
            raise ScheduleFetchError("Авторизация прошла, но страница расписания не содержит данных.")

        return response.text

    def _is_authorized_lessons_page(self, html: str) -> bool:
        return "table_lessons" in html and "Расписание" in html
