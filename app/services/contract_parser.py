"""
Парсинг .docx-договоров, загружаемых админом во вкладке «Договоры».

Извлекает:
- ФИО клиента (Заказчика) из шапки договора — для привязки к ученику
- даты начала/окончания оказания услуг
- общую стоимость услуг
- график платежей (номер, сумма, срок оплаты)
- контактные данные родителя из блока реквизитов в конце договора
  (ФИО, телефон, email, город/улица/дом — индекс и квартира используются
  только для очистки адреса и никуда не сохраняются)
"""
import re
from datetime import date
from typing import Optional

import docx

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

ORDINALS = {
    "первый": 1, "второй": 2, "третий": 3, "четвёртый": 4, "четвертый": 4, "пятый": 5,
    "шестой": 6, "седьмой": 7, "восьмой": 8, "девятый": 9, "десятый": 10,
    "одиннадцатый": 11, "двенадцатый": 12, "тринадцатый": 13, "четырнадцатый": 14,
    "пятнадцатый": 15, "шестнадцатый": 16, "семнадцатый": 17, "восемнадцатый": 18,
    "девятнадцатый": 19, "двадцатый": 20,
    "двадцать первый": 21, "двадцать второй": 22, "двадцать третий": 23,
    "двадцать четвёртый": 24, "двадцать четвертый": 24, "двадцать пятый": 25,
    "двадцать шестой": 26, "двадцать седьмой": 27, "двадцать восьмой": 28,
    "двадцать девятый": 29, "двадцать тридцатый": 30, "тридцатый": 30,
    "тридцать первый": 31, "тридцать второй": 32, "тридцать третий": 33,
    "тридцать четвёртый": 34, "тридцать четвертый": 34, "тридцать пятый": 35,
    "тридцать шестой": 36, "тридцать седьмой": 37, "тридцать восьмой": 38,
    "тридцать девятый": 39, "сороковой": 40,
}


def _parse_ru_date(text: str) -> Optional[date]:
    text = text.strip().rstrip("г.").strip()
    m = re.match(r"(\d{1,2})\s+(\S+)\s+(\d{4})", text)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def _parse_amount(text: str) -> Optional[float]:
    cleaned = re.sub(r"[^0-9,.]", "", text)
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_contract_docx(file_path: str) -> dict:
    d = docx.Document(file_path)
    full_text = "\n".join(p.text for p in d.paragraphs)

    result: dict = {
        "contract_number": None,
        "client_name_header": None,
        "start_date": None,
        "end_date": None,
        "total_amount": None,
        "payments": [],
        "parent_full_name": None,
        "parent_phone": None,
        "parent_email": None,
        "city": None,
        "street": None,
        "house": None,
        "parse_warnings": [],
    }

    m = re.search(r"ДОГОВОР\s*№\s*(\S+)", full_text)
    if m:
        result["contract_number"] = m.group(1).strip()

    m = re.search(r"с одной стороны,\s*(.+?),\s*именуем", full_text)
    if m:
        result["client_name_header"] = re.sub(r"\s+", " ", m.group(1)).strip()
    else:
        result["parse_warnings"].append("Не удалось найти ФИО заказчика в шапке договора")

    m = re.search(r"Начало оказания Услуг\s*[–-]\s*(.+?)(?:г\.|\n)", full_text)
    result["start_date"] = _parse_ru_date(m.group(1)) if m else None
    m = re.search(r"Дата окончания Услуг\s*[–-]\s*(.+?)(?:г\.|\n)", full_text)
    result["end_date"] = _parse_ru_date(m.group(1)) if m else None
    if result["start_date"] and result["end_date"] and result["end_date"] < result["start_date"]:
        result["parse_warnings"].append(
            "Дата окончания раньше даты начала — вероятно, опечатка в тексте договора"
        )

    m = re.search(r"Стоимость Услуг составляет\s*([\d\s\xa0\u202f]+,\d+)", full_text)
    result["total_amount"] = _parse_amount(m.group(1)) if m else None

    payments = []
    pattern = re.compile(
        r"Заказчик вносит (\S+(?:\s\S+)?) платёж\s*([\d\s\xa0\u202f]+,\d+).*?"
        r"не позднее\s*(\d{1,2}\s+\S+\s+\d{4})\s*года",
        re.IGNORECASE,
    )
    for match in pattern.finditer(full_text):
        ordinal_word, amount_text, date_text = match.groups()
        ordinal_word_clean = ordinal_word.strip().lower()
        number = ORDINALS.get(ordinal_word_clean)
        due_date = _parse_ru_date(date_text)
        amount = _parse_amount(amount_text)
        if number is None or due_date is None or amount is None:
            result["parse_warnings"].append(f"Не разобрана строка платежа: «{ordinal_word}»")
            continue
        payments.append({"number": number, "amount": amount, "due_date": due_date.isoformat()})
    result["payments"] = payments
    if not payments:
        result["parse_warnings"].append("Не найдено ни одного платежа в графике")

    if not d.tables:
        result["parse_warnings"].append("В документе не найдена таблица с реквизитами сторон")
        return result

    table = d.tables[-1]
    if len(table.rows) < 3:
        result["parse_warnings"].append("Таблица реквизитов имеет неожиданную структуру")
        return result

    cell_text = table.rows[2].cells[0].text
    lines = [l.strip() for l in cell_text.split("\n") if l.strip()]
    if lines:
        result["parent_full_name"] = lines[0]
    else:
        result["parse_warnings"].append("Не удалось найти ФИО в блоке реквизитов")

    phone_m = re.search(r"(\+375\d{9})", cell_text)
    email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", cell_text)
    result["parent_phone"] = phone_m.group(1) if phone_m else None
    result["parent_email"] = email_m.group(0) if email_m else None
    if not result["parent_phone"]:
        result["parse_warnings"].append("Не найден номер телефона в блоке реквизитов")
    if not result["parent_email"]:
        result["parse_warnings"].append("Не найден email в блоке реквизитов")

    address_line = None
    for l in lines[1:]:
        if l == result["parent_phone"]:
            continue
        if result["parent_email"] and result["parent_email"] in l:
            continue
        if l.startswith("_") or "Б.П." in l:
            continue
        address_line = l
        break

    if address_line:
        addr = address_line
        index_m = re.search(r"\b(\d{6})\b", addr)
        if index_m:
            addr = addr.replace(index_m.group(1), "")

        flat_m = re.search(r"кв\.?\s*(\d+)", addr, re.IGNORECASE)
        if flat_m:
            addr = addr[:flat_m.start()] + addr[flat_m.end():]

        addr = re.sub(r"^[,.\s]+|[,.\s]+$", "", addr)
        parts = [p.strip() for p in re.split(r"[,]", addr) if p.strip()]

        # Тип населённого пункта (город/деревня/агрогородок/посёлок и т.п.) —
        # приравниваем к городу, независимо от того, в каком по счёту сегменте
        # адреса он встретился.
        SETTLEMENT_PREFIX = re.compile(
            r"^(?:г\.?|гор\.?|город|д\.?|дер\.?|деревня|аг\.?|агрогородок|п\.?|пос\.?|посёлок|поселок|гп\.?)\s*(.+)$",
            re.IGNORECASE,
        )
        # Чисто районная часть адреса ("Минский р-н") — не город и не улица, отбрасываем.
        DISTRICT_ONLY = re.compile(r"^.+\s+р-?н\.?$|^.+\s+район$", re.IGNORECASE)

        city = None
        street_parts = []
        for part in parts:
            if DISTRICT_ONLY.match(part):
                continue
            sm = SETTLEMENT_PREFIX.match(part)
            if sm and city is None:
                city = sm.group(1).strip(" .")
                continue
            street_parts.append(part)

        street = None
        house = None
        if street_parts:
            last = street_parts[-1]
            hm = re.search(r"(\d[\d/]*)\s*$", last)
            if hm:
                house = hm.group(1)
                street = last[:hm.start()].strip(" .")
            else:
                street = last
            if len(street_parts) > 1:
                street = ", ".join(street_parts[:-1]) + (f", {street}" if street else "")

        result["city"] = city
        result["street"] = street
        result["house"] = house
        if not city:
            result["parse_warnings"].append(
                "Город не распознан однозначно — проверьте и поправьте вручную"
            )
    else:
        result["parse_warnings"].append("Не удалось найти строку адреса в блоке реквизитов")

    return result
