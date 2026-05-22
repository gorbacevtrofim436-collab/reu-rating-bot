from __future__ import annotations

import os
from dataclasses import dataclass

import requests


class ScheduleFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScheduleSuggestion:
    name: str
    key: str
    metadata: str | None = None


class ScheduleClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("REA_SCHEDULE_URL", "https://rasp.rea.ru").rstrip("/")
        self.timeout = float(os.getenv("REA_SCHEDULE_REQUEST_TIMEOUT", os.getenv("REA_REQUEST_TIMEOUT", "15")))
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                ),
                "Referer": f"{self.base_url}/",
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def search_suggestions(self, query: str) -> list[ScheduleSuggestion]:
        query = query.strip()
        if not query:
            return []

        try:
            response = self.session.get(
                f"{self.base_url}/Schedule/SearchBarSuggestions",
                params={"searchFor": query},
                timeout=self.timeout,
            )
            self._raise_for_status(response)
            payload = response.json()
        except requests.RequestException as exc:
            raise ScheduleFetchError("Сайт расписания РЭУ временно не отвечает.") from exc
        except ValueError as exc:
            raise ScheduleFetchError("Сайт расписания вернул неожиданный формат данных.") from exc

        suggestions: list[ScheduleSuggestion] = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            key = str(item.get("key") or "").strip()
            metadata = str(item.get("metadata") or "").strip() or None
            if name and key:
                suggestions.append(ScheduleSuggestion(name=name, key=key, metadata=metadata))
        return suggestions

    def fetch_week_html(self, selection_key: str, *, week_num: int = -1) -> str:
        selection_key = selection_key.strip()
        if not selection_key:
            raise ScheduleFetchError("Не задана группа для расписания.")

        try:
            response = self.session.get(
                f"{self.base_url}/Schedule/ScheduleCard",
                params={"selection": selection_key, "weekNum": week_num, "catfilter": ""},
                timeout=self.timeout,
            )
            self._raise_for_status(response)
        except requests.RequestException as exc:
            raise ScheduleFetchError("Не удалось получить расписание с сайта РЭУ.") from exc

        html = response.text
        if "id=\"weekNum\"" not in html and "dayh" not in html:
            raise ScheduleFetchError("Сайт расписания не вернул таблицу занятий.")
        return html

    def _raise_for_status(self, response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise ScheduleFetchError(f"Ошибка сайта расписания: HTTP {response.status_code}") from exc
