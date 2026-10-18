"""SSH-only read-only export of safe Agent Tools internal notices."""

from contextlib import closing
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
from dotenv import dotenv_values
key = dotenv_values('/opt/mcpserver/.env').get('PRIVACY_SUPPRESSION_KEY')
if not key:
    raise RuntimeError('Privacy guard unavailable; no notice export')
os.environ['PRIVACY_SUPPRESSION_KEY'] = key
from directory import db, notices

with closing(db.connect(read_only=True)) as conn:
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    feed = notices.export_feed(conn)
    conn.rollback()
print(json.dumps(feed, ensure_ascii=False, separators=(',', ':')))