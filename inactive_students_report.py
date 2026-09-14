"""
Разовый скрипт (запускается у вас на компьютере, сайт не трогает).

Ищет учеников, у которых последнее занятие со статусом "проведено" было
больше N дней назад (или таких занятий не было вообще), и сохраняет
результат в Excel-файл рядом со скриптом.

УСТАНОВКА (один раз):
    pip install psycopg2-binary openpyxl

ГДЕ ВЗЯТЬ СТРОКУ ПОДКЛЮЧЕНИЯ К БАЗЕ:
    1. Зайдите на dashboard.render.com -> ваша база данных Postgres
       (не веб-сервис pifagor-api, а именно база данных)
    2. Найдите раздел "Connections" -> "External Database URL"
       (не "Internal" — тот работает только внутри Render)
    3. Скопируйте строку целиком, она выглядит примерно так:
       postgresql://user:password@dpg-xxxxx.oregon-postgres.render.com/dbname

ЗАПУСК:
    python inactive_students_report.py "postgresql://user:password@host/dbname"

    Порог в днях по умолчанию 5, можно указать другой вторым аргументом:
    python inactive_students_report.py "postgresql://..." 7
"""
import sys
from datetime import date, timedelta

try:
    import psycopg2
except ImportError:
    print("Не установлена библиотека psycopg2. Выполните: pip install psycopg2-binary")
    sys.exit(1)

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font
except ImportError:
    print("Не установлена библиотека openpyxl. Выполните: pip install openpyxl")
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Использование: python inactive_students_report.py \"<строка подключения к БД>\" [дней=5]")
        sys.exit(1)

    db_url = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    cutoff = date.today() - timedelta(days=days)

    print(f"Подключаюсь к базе...")
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    cur.execute("""
        SELECT
            cp.id,
            u.last_name,
            u.first_name,
            cp.crm_status,
            (
                SELECT MAX(l.date)
                FROM lessons l
                WHERE l.child_id = cp.id AND l.status = 'completed'
            ) AS last_lesson_date
        FROM child_profiles cp
        JOIN users u ON u.id = cp.user_id
        ORDER BY u.last_name, u.first_name
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"Всего учеников в базе: {len(rows)}")

    result = []
    for child_id, last_name, first_name, crm_status, last_lesson_date in rows:
        if last_lesson_date is None or last_lesson_date < cutoff:
            days_since = (date.today() - last_lesson_date).days if last_lesson_date else None
            result.append((
                f"{last_name} {first_name}".strip(),
                crm_status,
                last_lesson_date.isoformat() if last_lesson_date else "никогда",
                days_since if days_since is not None else "",
            ))

    result.sort(key=lambda r: (r[3] == "", -(r[3] if isinstance(r[3], int) else 0)))

    print(f"Не занимаются {days}+ дней (или ни разу): {len(result)}")

    wb = Workbook()
    ws = wb.active
    ws.title = "Не занимаются"
    headers = ["Ученик", "Статус в CRM", "Последнее проведённое занятие", "Дней без занятий"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in result:
        ws.append(row)
    for col_cells in ws.columns:
        width = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10) + 2
        ws.column_dimensions[col_cells[0].column_letter].width = max(width, 12)

    filename = f"inactive-students-{date.today().isoformat()}.xlsx"
    wb.save(filename)
    print(f"Готово! Файл сохранён рядом со скриптом: {filename}")


if __name__ == "__main__":
    main()
