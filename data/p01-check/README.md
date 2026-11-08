# Контрольный тест P01

В основном демонстрационном наборе P01 есть 40 вопросов по пяти документам. Каждый вопрос имеет эталон и дословную цитату. Файл `control_answers.csv` содержит подготовленные ответы для проверки оценщика без модели.

## Запуск в дашборде

На странице **Тестовые наборы** нажмите **Запустить тест P01**. Дашборд возьмёт документы и вопросы из `data/public`, включит итоговую оценку и добавит запуск в историю. После завершения откройте отчёт из списка обработок.

## Проверка контрольных ответов без API

Выполните из корня проекта в PowerShell.

```powershell
New-Item -ItemType Directory -Force -Path runs | Out-Null
.\.venv\Scripts\python.exe -c 'import json; from pathlib import Path; gold = json.loads(Path("data/public/answers_public.json").read_text(encoding="utf-8")); Path("runs/p01-gold.json").write_text(json.dumps([row for row in gold if row["package"] == "P01"]), encoding="utf-8")'
.\.venv\Scripts\python.exe -X utf8 src/evaluation/check.py data/p01-check/control_answers.csv --gold runs/p01-gold.json --packages data/public/packages
```

Ожидаемый результат для подготовленных ответов — 40 верных из 40 и индекс 100.0. Результат прогона модели зависит от её ответов.
