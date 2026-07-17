.PHONY: help install dev-install test lint format clean

PYTHON ?= python3
PIP ?= pip3

help:  ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Установить пакет в текущее окружение (разработка)
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

dev-install:  ## Установить с dev-зависимостями
	$(PIP) install -r requirements.txt
	$(PIP) install -e ".[dev]"

test:  ## Прогнать тесты
	$(PYTHON) -m pytest -v

lint:  ## Линтинг
	$(PYTHON) -m ruff check voxnode/ tools/ || true

format:  ## Форматирование
	$(PYTHON) -m ruff format voxnode/ tools/ || true

clean:  ## Очистить артефакты сборки
	rm -rf build/ dist/ *.egg-info voxnode.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
