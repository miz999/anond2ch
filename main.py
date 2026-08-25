import os
import re
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# 設定
RSS_URL = "https://anond.hatelabo.jp/rss"
DB_PATH = "anond.db"
# --- 設定の変更 ---
# OUTPUT_DIR = "."  # 必要に応じて "output" 等に変更
OUTPUT_DIR = "dat"   # 「dat」フォルダ内に格納するように変更（名前はお好みでOK）


def parse_date(date_str):
    """ISO8601形式の日付文字列を2ch風フォーマットに変換"""
    try:
        dt = datetime.fromisoformat(date_str)
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        return dt.strftime(f"%Y/%m/%d({weekdays[dt.weekday()]}) %H:%M:%S")
    except Exception:
        return date_str


def sanitize_body(html_content):
    """本文のHTMLタグ除去と改行変換"""
    if not html_content:
        return ""
    text = re.sub(r'</p>\s*<p>', '<br>', html_content)
    text = re.sub(r'<br\s*/?>', '<br>', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('\r', '').replace('\n', '')
    return text.strip()


def init_db(db_path=DB_PATH):
    """
    anond.db の存在確認を行い、ファイルが存在しない（消えた）場合のみ
    新規作成およびスキーマ定義・初期化を実行する
    """
    if not os.path.exists(db_path):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {db_path} が存在しません。新規作成して初期化します...")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # テーブル作成
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                is_placeholder INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS posts (
                entry_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                res_number INTEGER NOT NULL,
                target_entry_id TEXT,
                target_res_num INTEGER,
                body TEXT NOT NULL,
                posted_at TEXT NOT NULL,
                raw_date TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id),
                UNIQUE (thread_id, res_number)
            )
        ''')

        # インデックス作成
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_thread_id ON posts (thread_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_target_entry_id ON posts (target_entry_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_threads_updated_at ON threads (updated_at DESC)')

        conn.commit()
        conn.close()
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {db_path} の初期化が完了しました。")
    else:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 既存の {db_path} を確認しました。")


def process_rss(conn):
    """RSSを取得してDBに格納・ツリー解決"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] RSSを取得中...")
    req = urllib.request.Request(RSS_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        xml_data = response.read()

    root = ET.fromstring(xml_data)
    ns = {
        'rss': 'http://purl.org/rss/1.0/',
        'dc': 'http://purl.org/dc/elements/1.1/',
        'content': 'http://purl.org/rss/1.0/modules/content/'
    }

    items = list(reversed(root.findall('rss:item', ns)))
    cursor = conn.cursor()

    new_posts_count = 0

    for item in items:
        title_elem = item.find('rss:title', ns)
        link_elem = item.find('rss:link', ns)
        content_elem = item.find('content:encoded', ns)
        date_elem = item.find('dc:date', ns)

        title = title_elem.text if title_elem is not None else ""
        link = link_elem.text if link_elem is not None else ""
        entry_id = link.rstrip('/').split('/')[-1]
        raw_date = date_elem.text if date_elem is not None else ""
        posted_at = parse_date(raw_date)

        body_raw = content_elem.text if content_elem is not None else ""
        body = sanitize_body(body_raw)

        # 既にDBに存在する記事はスキップ
        cursor.execute("SELECT 1 FROM posts WHERE entry_id = ?", (entry_id,))
        if cursor.fetchone():
            continue

        # ---------------------------------------------------------
        # パターン1: 親記事 (タイトルが anond: で始まらない)
        # ---------------------------------------------------------
        if not title.startswith('anond:'):
            thread_title = title if title != '■' else (body[:30] + '...' if len(body) > 30 else body)

            cursor.execute("SELECT is_placeholder FROM threads WHERE thread_id = ?", (entry_id,))
            row = cursor.fetchone()

            if row:
                cursor.execute("""
                    UPDATE threads 
                    SET title = ?, is_placeholder = 0, updated_at = ? 
                    WHERE thread_id = ?
                """, (thread_title, raw_date, entry_id))
            else:
                cursor.execute("""
                    INSERT INTO threads (thread_id, title, is_placeholder, created_at, updated_at)
                    VALUES (?, ?, 0, ?, ?)
                """, (entry_id, thread_title, raw_date, raw_date))

            cursor.execute("""
                INSERT OR IGNORE INTO posts 
                (entry_id, thread_id, res_number, target_entry_id, target_res_num, body, posted_at, raw_date)
                VALUES (?, ?, 1, NULL, NULL, ?, ?, ?)
            """, (entry_id, entry_id, body, posted_at, raw_date))

            new_posts_count += 1

        # ---------------------------------------------------------
        # パターン2: 子記事 (タイトルが anond:YYYYMMDDHHMMSS)
        # ---------------------------------------------------------
        else:
            target_entry_id = title.replace('anond:', '').strip()

            cursor.execute("SELECT thread_id, res_number FROM posts WHERE entry_id = ?", (target_entry_id,))
            target_info = cursor.fetchone()

            if target_info:
                thread_id, target_res_num = target_info
            else:
                # 参照先がまだ存在しない（迷子レス）場合
                thread_id = target_entry_id
                target_res_num = 1

                cursor.execute("SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,))
                if not cursor.fetchone():
                    placeholder_title = f"anond:{thread_id} (過去記事参照)"
                    cursor.execute("""
                        INSERT INTO threads (thread_id, title, is_placeholder, created_at, updated_at)
                        VALUES (?, ?, 1, ?, ?)
                    """, (thread_id, placeholder_title, raw_date, raw_date))

            cursor.execute("SELECT MAX(res_number) FROM posts WHERE thread_id = ?", (thread_id,))
            max_res = cursor.fetchone()[0]
            next_res_num = (max_res + 1) if max_res is not None else 1

            cursor.execute("""
                INSERT INTO posts 
                (entry_id, thread_id, res_number, target_entry_id, target_res_num, body, posted_at, raw_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (entry_id, thread_id, next_res_num, target_entry_id, target_res_num, body, posted_at, raw_date))

            cursor.execute("UPDATE threads SET updated_at = ? WHERE thread_id = ?", (raw_date, thread_id))
            new_posts_count += 1

    conn.commit()
    print(f"DB更新完了: 新規 {new_posts_count} 件の投稿を追加しました。")


def export_dat_and_subject(conn):
    """DBの内容から subject.txt および *.dat を UTF-8 で一括生成"""
    
    # 【追加】出力先ディレクトリが存在しない場合は自動作成
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    cursor = conn.cursor()

    cursor.execute("""
        SELECT t.thread_id, t.title 
        FROM threads t
        ORDER BY t.updated_at DESC
    """)
    threads = cursor.fetchall()

    subject_lines = []

    for thread_id, title in threads:
        cursor.execute("""
            SELECT res_number, body, posted_at, target_res_num 
            FROM posts 
            WHERE thread_id = ? 
            ORDER BY res_number ASC
        """, (thread_id,))
        posts = cursor.fetchall()

        if not posts:
            continue

        dat_filename = f"{thread_id}.dat"
        dat_path = os.path.join(OUTPUT_DIR, dat_filename)
        dat_lines = []

        for res_num, body, posted_at, target_res_num in posts:
            thread_title_field = title if res_num == 1 else ""

            if target_res_num is not None:
                body_formatted = f">>{target_res_num}<br>{body}"
            else:
                body_formatted = body

            # 「増田さん」名義で出力
            line = f"増田さん<>sage<>{posted_at} ID:anond<>{body_formatted}<>{thread_title_field}"
            dat_lines.append(line)

        with open(dat_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(dat_lines) + "\n")

        subject_lines.append(f"{dat_filename}<>{title} ({len(posts)})")

    subject_path = os.path.join(OUTPUT_DIR, "subject.txt")
    with open(subject_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(subject_lines) + "\n")

    print(f"ファイル出力完了: {len(subject_lines)} 件のスレッドを subject.txt に出力しました。\n")


def main():
    # 存在チェックと必要時の初期化
    init_db(DB_PATH)

    # 接続して更新・出力処理
    conn = sqlite3.connect(DB_PATH)
    try:
        process_rss(conn)
        export_dat_and_subject(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()