#!/usr/bin/env python3
"""Convert a Google Takeout subscriptions.csv to YouTube CRT's subscriptions.txt.

    python tools/import_subscriptions.py subscriptions.csv > subscriptions.txt

Takeout columns: Channel ID, Channel URL, Channel title.
"""
import csv
import re
import sys


def main(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.reader(f):
            if not row or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', row[0].strip()):
                continue
            title = row[2].replace('|', '/').strip() if len(row) > 2 else ''
            print('%s | %s' % (title, row[0].strip()) if title else row[0].strip())


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
