# -*- coding: utf-8 -*-
"""
Connection settings (direct PostgreSQL).  PASSWORD KABHI FILE MEIN MAT LIKHO - environment variables use karo:

  export SRC_PGHOST=...   SRC_PGPORT=5432  SRC_PGDATABASE=AKG_Hardware  SRC_PGUSER=...  SRC_PGPASSWORD=...
  export DST_PGHOST=...   DST_PGPORT=5432  DST_PGDATABASE=AKG_PROD      DST_PGUSER=...  DST_PGPASSWORD=...

Ya isi folder mein 'local_config.py' banao jis mein SRC, DST dicts hon (git mein commit mat karna).
SRC = Odoo 16 (sirf PADHA jata hai, connection read-only hota hai).  DST = Odoo 20 PROD (yahan likha jata hai).
"""
import os

SRC = dict(host=os.getenv("SRC_PGHOST", "localhost"), port=int(os.getenv("SRC_PGPORT", "5432")),
           dbname=os.getenv("SRC_PGDATABASE", "AKG_Hardware"), user=os.getenv("SRC_PGUSER", "odoo"),
           password=os.getenv("SRC_PGPASSWORD", ""))
DST = dict(host=os.getenv("DST_PGHOST", "localhost"), port=int(os.getenv("DST_PGPORT", "5432")),
           dbname=os.getenv("DST_PGDATABASE", ""), user=os.getenv("DST_PGUSER", "odoo"),
           password=os.getenv("DST_PGPASSWORD", ""))

# Odoo 16 ki base language (translatable fields ki asal value is language ki hoti hai). Odoo 20 mein {"en_US": "..."} bante hain.
BASE_LANG = os.getenv("BASE_LANG", "en_US")

# Master script ka idmap.json (model -> {src_id: dst_id}). PROD wala hona chahiye, UAT wala NAHI.
MASTER_IDMAP = os.getenv("MASTER_IDMAP", "idmap.json")

try:
    from local_config import SRC, DST  # noqa
except ImportError:
    pass
