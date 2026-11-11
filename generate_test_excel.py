from openpyxl import Workbook

HEADERS = ["title", "rooms", "price", "currency", "district", "pets_allowed", "photo_urls"]

ROWS = [
    ["Студия у моря", 1, 500, "USD", "My Khe", "да", ""],
    ["Двушка в центре", 2, 1200, "USD", "An Thuong", "нет", ""],
    ["Дом с бассейном", 3, 80000000, "VND", "Son Tra", "да", ""],
]


def main():
    wb = Workbook()
    ws = wb.active
    ws.title = "Listings"

    ws.append(HEADERS)
    for row in ROWS:
        ws.append(row)

    wb.save("test_listings.xlsx")


if __name__ == "__main__":
    main()
