# -*- coding: utf-8 -*-
"""
Generic DIRECT-DATABASE engine: Odoo 16 (PostgreSQL, read-only)  ->  Odoo 20 (PostgreSQL, write).

Idea: ORM/XML-RPC/JSON2 ko bypass karke table-to-table copy.
  * columns dono databases ke information_schema se milte hain (jo column dono mein hai wahi copy hota hai)
  * many2one ids idmap se badalte hain (master ids = idmap.json, transactions = dst DB ki akg_idmap table)
  * translatable char/text: Odoo 16 ka plain text -> Odoo 20 ka jsonb {"en_US": "..."}
  * company-dependent fields: Odoo 16 ki ir_property -> Odoo 20 ka jsonb column
  * many2many relation tables copy
  * header + lines ek hi SAVEPOINT mein (poora document ya kuch nahi); fail ho to document ek ek karke
  * akg_idmap table DST DB ke andar hai, isi transaction mein likhi jati hai => crash par duplicate nahi bante
Source values EXACT copy hoti hain (debit/credit/balance/amounts recompute nahi hote).
"""
import collections
import csv
import json
import logging
import os
import re
import sys
import time

import psycopg2
import psycopg2.extras
from psycopg2 import sql
from psycopg2.extras import Json, execute_values

log = logging.getLogger("txdb")

CANON = {"stock.location.route": "stock.route"}
# source relation -> dst relation: model badal gaya lekin map (idmap) src model ke naam se hi chalta hai
REL_OK = {("hr.leave.type", "hr.work.entry.type"), ("hr.contract", "hr.version")}
# Odoo 16 -> Odoo 20 known column renames (sirf tab lagti hain jab naya naam dst mein ho aur purana na ho)
AUTO_RENAMES = {
    "product_uom": "product_uom_id",          # sale.order.line / purchase.order.line
    "groups_id": "group_ids",
    "holiday_status_id": "work_entry_type_id",
    "acc_number": "account_number",
    "acc_holder_name": "holder_name",
}
GLOBAL_SKIP = {"id", "create_uid", "write_uid", "display_name", "__last_update", "parent_path", "password",
               "access_token", "message_main_attachment_id", "signup_token", "signup_type", "signup_expiration"}
# jin models ke records transaction run mein migrate nahi hote: unke unresolved links ko pending mein mat daalo
NEVER_MIGRATED = {"stock.move", "stock.move.line", "stock.picking", "stock.valuation.layer", "mail.message",
                  "mail.activity", "mail.followers", "stock.scrap", "stock.landed.cost", "account.asset",
                  "purchase.requisition", "mrp.production", "ir.attachment", "stock.quant.package"}
# company-dependent fields jin ki "default" property (res_id NULL) har record par explicit lagani hai
PROP_DEFAULT_FIELDS = {"property_account_receivable_id", "property_account_payable_id"}


def canon(m):
    return CANON.get(m, m)


def family(t):
    t = (t or "").lower()
    if t in ("character varying", "text", "character", "citext"):
        return "text"
    if t in ("integer", "bigint", "smallint"):
        return "int"
    if t in ("numeric", "double precision", "real"):
        return "num"
    if t == "boolean":
        return "bool"
    if t == "date":
        return "date"
    if t.startswith("timestamp"):
        return "ts"
    if t in ("jsonb", "json"):
        return "json"
    if t == "bytea":
        return "bytes"
    return t


def ident(x):
    return sql.Identifier(x)


# ============================================================================ database wrapper
class DB:
    def __init__(self, cfg, label, readonly=False):
        self.cfg, self.label, self.readonly = cfg, label, readonly
        self.cx = psycopg2.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"], user=cfg["user"],
                                   password=cfg["password"], application_name="akg_tx_direct", connect_timeout=20)
        self.cx.set_session(readonly=readonly, autocommit=bool(readonly))   # source: autocommit (koi lamba transaction nahi)
        self._cols = None
        self._fields, self._sel, self._n = {}, {}, {}

    def cur(self, named=None):
        return self.cx.cursor(name=named, cursor_factory=psycopg2.extras.RealDictCursor)

    def q(self, query, args=None):
        with self.cur() as c:
            c.execute(query, args)
            return c.fetchall()

    def q1(self, query, args=None):
        r = self.q(query, args)
        return r[0] if r else None

    def x(self, query, args=None):
        with self.cur() as c:
            c.execute(query, args)
            return c.rowcount

    def ev(self, query, rows, page=1000):
        with self.cur() as c:
            execute_values(c, query, rows, page_size=page)

    def scalar(self, query, args=None):
        with self.cx.cursor() as c:
            c.execute(query, args)
            r = c.fetchone()
            return r[0] if r else None

    # ---- schema
    def columns(self, table):
        if self._cols is None:
            self._cols = collections.defaultdict(dict)
            for r in self.q("select table_name t, column_name c, data_type d, is_nullable n, column_default df "
                            "from information_schema.columns where table_schema = current_schema()"):
                self._cols[r["t"]][r["c"]] = dict(type=r["d"], fam=family(r["d"]), nullable=(r["n"] == "YES"),
                                                  default=r["df"] is not None)
        return self._cols.get(table, {})

    def has_table(self, table):
        return bool(self.columns(table))

    def refresh_schema(self):
        self._cols = None

    def fields(self, model):
        """ir_model_fields -> {name: dict(ttype, relation, store, translate, required, relation_table, column1, column2)}"""
        if model not in self._fields:
            rows = self.q("select name, ttype, relation, store, required, translate::text as tr, relation_table, "
                          "column1, column2 from ir_model_fields where model = %s", (model,))
            self._fields[model] = {r["name"]: dict(
                ttype=r["ttype"], relation=r["relation"], store=bool(r["store"]), required=bool(r["required"]),
                translate=str(r["tr"]).lower() not in ("false", "f", "none", "", "null"),
                relation_table=r["relation_table"], column1=r["column1"], column2=r["column2"]) for r in rows}
        return self._fields[model]

    def selection(self, model, field):
        k = (model, field)
        if k not in self._sel:
            rows = self.q("select s.value from ir_model_fields_selection s join ir_model_fields f on f.id = s.field_id "
                          "where f.model = %s and f.name = %s", (model, field))
            self._sel[k] = {r["value"] for r in rows}
        return self._sel[k]

    def table_of(self, model):
        return model.replace(".", "_")

    def version(self):
        r = self.q1("select latest_version v from ir_module_module where name = 'base'")
        return (r or {}).get("v") or "?"

    def sequence_of(self, table):
        return self.scalar("select pg_get_serial_sequence(%s, 'id')", (table,))


# ============================================================================ id map (lives inside DST db)
class IdMap:
    def __init__(self, dst, create=True):
        self.dst = dst
        self.create = create
        if create:
            self._ddl(dst)
        self.cache = {}
        self.stage, self.stage_rows, self.stage_pend = {}, [], []

    def _ddl(self, dst):
        dst.x("create table if not exists akg_idmap(model text not null, src_id bigint not null, dst_id bigint not null,"
              " created boolean not null default true, primary key (model, src_id))")
        dst.x("create index if not exists akg_idmap_dst_idx on akg_idmap(model, dst_id)")
        dst.x("create table if not exists akg_pending(dst_table text not null, dst_id bigint not null, col text not null,"
              " rel text not null, src_id bigint not null)")
        dst.x("create index if not exists akg_pending_idx on akg_pending(rel, src_id)")

    # ---- reads
    def _load(self, model):
        if not self.create and not self.dst.has_table("akg_idmap"):
            return {}
        if model not in self.cache:
            self.cache[model] = {r["src_id"]: r["dst_id"] for r in
                                 self.dst.q("select src_id, dst_id from akg_idmap where model = %s", (model,))}
        return self.cache[model]

    def get(self, model, sid):
        if sid is None:
            return None
        m = canon(model)
        v = self.stage.get((m, sid))
        if v is not None:
            return v
        return self._load(m).get(sid)

    def done(self, model):
        return set(self._load(canon(model)))

    def dst_ids(self, model):
        return set(self._load(canon(model)).values())

    # ---- writes (stage -> flush inside savepoint -> accept / reject)
    def put(self, model, sid, did, created=True):
        m = canon(model)
        self.stage[(m, sid)] = did
        self.stage_rows.append((m, sid, did, created))

    def unput(self, model, sid):
        m = canon(model)
        self.stage.pop((m, sid), None)
        self.stage_rows = [r for r in self.stage_rows if not (r[0] == m and r[1] == sid)]

    def add_pending(self, dst_table, dst_id, col, rel, src_id):
        self.stage_pend.append((dst_table, dst_id, col, canon(rel), src_id))

    def flush(self):
        if self.stage_rows:
            self.dst.ev("insert into akg_idmap(model, src_id, dst_id, created) values %s "
                           "on conflict (model, src_id) do update set dst_id = excluded.dst_id",
                           self.stage_rows, 2000)
        if self.stage_pend:
            self.dst.ev("insert into akg_pending(dst_table, dst_id, col, rel, src_id) values %s",
                           self.stage_pend, 2000)

    def accept(self):
        for (m, s, d, _c) in self.stage_rows:
            self._load(m)[s] = d
        self.stage, self.stage_rows, self.stage_pend = {}, [], []

    def reject(self):
        self.stage, self.stage_rows, self.stage_pend = {}, [], []

    def set_direct(self, model, sid, did, created=False):
        """check --fix / import: foran likho (stage ke bina)"""
        m = canon(model)
        self.dst.x("insert into akg_idmap(model, src_id, dst_id, created) values (%s,%s,%s,%s) "
                   "on conflict (model, src_id) do update set dst_id = excluded.dst_id", (m, sid, did, created))
        self._load(m)[sid] = did

    # ---- import / export of master idmap.json
    def import_json(self, path):
        if not os.path.exists(path):
            log.warning("idmap file nahi mili: %s (masters ka map khali maana jayega)", path)
            return 0
        data = json.load(open(path, encoding="utf-8"))
        rows = []
        for model, mp in data.items():
            if model.startswith("_") or not isinstance(mp, dict):
                continue
            for s, d in mp.items():
                try:
                    rows.append((canon(model), int(s), int(d), False))
                except (TypeError, ValueError):
                    continue
        if rows:
            self.dst.ev("insert into akg_idmap(model, src_id, dst_id, created) values %s "
                           "on conflict (model, src_id) do update set dst_id = excluded.dst_id "
                           "where akg_idmap.created = false", rows, 5000)
        self.cache = {}
        return len(rows)

    def export_json(self, path):
        out = collections.defaultdict(dict)
        for r in self.dst.q("select model, src_id, dst_id from akg_idmap order by model, src_id"):
            out[r["model"]][str(r["src_id"])] = r["dst_id"]
        tmp = path + ".tmp"
        json.dump(out, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, path)
        return sum(len(v) for v in out.values())

    def resolve_pending(self):
        """Jin pending links ka target ab map ho chuka hai unhe SET-BASED UPDATE se lagao"""
        combos = self.dst.q("select distinct dst_table, col, rel from akg_pending")
        total = 0
        for c in combos:
            n = self.dst.x(
                sql.SQL("update {t} x set {c} = m.dst_id from akg_pending p join akg_idmap m "
                        "on m.model = p.rel and m.src_id = p.src_id "
                        "where p.dst_table = %s and p.col = %s and p.rel = %s and x.id = p.dst_id")
                .format(t=ident(c["dst_table"]), c=ident(c["col"])).as_string(self.dst.cx),
                (c["dst_table"], c["col"], c["rel"]))
            total += n
        self.dst.x("delete from akg_pending p using akg_idmap m where m.model = p.rel and m.src_id = p.src_id")
        return total


# ============================================================================ spec objects
class Child:
    def __init__(self, model, fk, dst_model=None, strict=(), hook=None, skip=(), rename=None, src_table=None,
                 dst_table=None, extra_read=(), sel_map=None):
        self.model, self.fk, self.dst_model = model, fk, dst_model or model
        self.strict, self.hook, self.skip = set(strict), hook, set(skip)
        self.rename = rename or {}
        self.src_table, self.dst_table = src_table, dst_table
        self.extra_read, self.sel_map = list(extra_read), sel_map or {}


class Spec:
    def __init__(self, name, model, desc="", dst_model=None, strict=(), children=(), hook=None, post=None,
                 skip=(), rename=None, where=None, date_col=None, extra_read=(), batch=200, compare=(),
                 runner=None, group="core", sel_map=None, src_table=None, dst_table=None, m2m_skip=()):
        self.name, self.model, self.desc = name, model, desc
        self.dst_model = dst_model or model
        self.strict, self.children = set(strict), list(children)
        self.hook, self.post, self.skip = hook, post, set(skip)
        self.rename = rename or {}
        self.where, self.date_col = where, date_col
        self.extra_read, self.batch, self.compare = list(extra_read), batch, list(compare)
        self.runner, self.group = runner, group
        self.sel_map = sel_map or {}       # {field: {src_value: dst_value}}
        self.src_table, self.dst_table = src_table, dst_table
        self.m2m_skip = set(m2m_skip)


class Col:
    __slots__ = ("sc", "dc", "kind", "rel", "allowed", "cd", "nullable", "default", "fam", "ttype")

    def __init__(self, **k):
        for a in self.__slots__:
            setattr(self, a, k.get(a))


class Plan:
    def __init__(self, cols, dropped, missing_required, src_table, dst_table, smodel, dmodel, rename):
        self.cols, self.dropped, self.missing_required = cols, dropped, missing_required
        self.src_table, self.dst_table, self.smodel, self.dmodel, self.rename = src_table, dst_table, smodel, dmodel, rename
        self.src_cols = [c.sc for c in cols if c.kind != "prop"]
        self.by_dc = {c.dc: c for c in cols}


class Ctx:
    """hooks ko diya jata hai"""

    def __init__(self, eng):
        self.eng, self.src, self.dst, self.args = eng, eng.src, eng.dst, eng.args
        self.dcols_cache = {}

    def resolve(self, rel, sid):
        return self.eng.resolve(rel, sid)

    def has(self, table, col):
        return col in self.dst.columns(table)

    def insert(self, table, vals):
        """extra row (hooks ke liye): id sequence se, create_uid/dates default"""
        return self.eng.insert_one(table, vals)

    def warn(self, msg, *a):
        log.warning(msg, *a)

    def stat(self, key, n=1):
        self.eng.stats[key] += n

    def m2m_insert(self, model, field, id1, ids2):
        f = self.dst.fields(model).get(field)
        if not f or not f.get("relation_table") or not ids2:
            return
        self.dst.ev(sql.SQL("insert into {t} ({a},{b}) values %s on conflict do nothing").format(
            t=ident(f["relation_table"]), a=ident(f["column1"]), b=ident(f["column2"])).as_string(self.dst.cx),
            [(id1, i) for i in ids2])


# ============================================================================ engine
class Engine:
    def __init__(self, src, dst, args, create_tables=True):
        self.src, self.dst, self.args = src, dst, args
        self.idmap = IdMap(dst, create=create_tables)
        self.chunk = {}
        self.stats = collections.Counter()
        self.missing = collections.Counter()
        self.results = []
        self.renamed = []
        self._xml = {}
        self._plans = {}
        self._prop_cache = {}
        self._srccd = None
        self._company = None
        self.ctx = Ctx(self)
        self.base_lang = getattr(args, "base_lang", "en_US")
        self.pending_ok = set()
        self.t0 = time.time()

    # ------------------------------------------------------------------ resolve
    def xmlid_map(self, rel):
        rel = canon(rel)
        if rel not in self._xml:
            out = {}
            try:
                s = self.src.q("select module, name, res_id from ir_model_data where model = %s", (rel,))
                d = self.dst.q("select module, name, res_id from ir_model_data where model = %s", (rel,))
                dm = {(r["module"], r["name"]): r["res_id"] for r in d}
                out = {r["res_id"]: dm[(r["module"], r["name"])] for r in s if (r["module"], r["name"]) in dm}
            except Exception as e:   # noqa
                log.warning("xmlid map %s fail: %s", rel, str(e)[:100])
            self._xml[rel] = out
        return self._xml[rel]

    def resolve(self, rel, sid):
        if sid is None:
            return None
        v = self.idmap.get(rel, sid)
        if v is not None:
            return v
        return self.xmlid_map(rel).get(sid)

    def default_company(self):
        if self._company is None:
            r = self.dst.q1("select id from res_company order by id limit 1")
            self._company = r["id"] if r else 1
        return self._company

    # ------------------------------------------------------------------ company dependent (src ir_property)
    def src_cd_fields(self):
        if self._srccd is None:
            self._srccd = collections.defaultdict(set)
            try:
                for r in self.src.q("select distinct f.model, f.name from ir_property p join ir_model_fields f on f.id = p.fields_id"):
                    self._srccd[r["model"]].add(r["name"])
            except Exception as e:  # noqa
                log.warning("ir_property nahi mili: %s", str(e)[:100])
        return self._srccd

    def load_props(self, model, field, src_ids):
        """{src_id: {dst_company_id(str): value}} ; value m2o ho to dst id"""
        f = self.src.fields(model).get(field, {})
        rows = self.src.q(
            "select p.res_id, p.company_id, p.type, p.value_float, p.value_integer, p.value_text, p.value_reference, "
            "p.value_datetime from ir_property p join ir_model_fields f on f.id = p.fields_id "
            "where f.model = %s and f.name = %s", (model, field))
        want = set(src_ids)
        out, default = collections.defaultdict(dict), {}
        for r in rows:
            t = r["type"]
            if t == "many2one" or r["value_reference"]:
                ref = r["value_reference"] or ""
                if "," not in ref:
                    continue
                rel, rid = ref.rsplit(",", 1)
                val = self.resolve(rel, int(rid))
                if val is None:
                    self.stats["prop_unresolved %s.%s" % (model, field)] += 1
                    continue
            elif t in ("float", "monetary"):
                val = r["value_float"]
            elif t in ("integer",):
                val = r["value_integer"]
            elif t == "boolean":
                val = bool(r["value_integer"])
            elif t in ("char", "text", "selection", "html"):
                val = r["value_text"]
            elif t in ("datetime", "date"):
                val = r["value_datetime"].isoformat() if r["value_datetime"] else None
            else:
                continue
            if val is None:
                continue
            comp = self.resolve("res.company", r["company_id"]) if r["company_id"] else None
            comp = comp or self.default_company()
            if not r["res_id"]:
                default[str(comp)] = val
                continue
            if not r["res_id"].startswith(model + ","):
                continue
            sid = int(r["res_id"].split(",")[1])
            if sid in want:
                out[sid][str(comp)] = val
        if field in PROP_DEFAULT_FIELDS and default:
            for sid in want:
                for c, v in default.items():
                    out[sid].setdefault(c, v)
        return out

    # ------------------------------------------------------------------ plan
    def table(self, db, model, override=None):
        return override or db.table_of(model)

    def build_plan(self, smodel, dmodel, rename=None, skip=(), drop_dst_cols=(), src_table=None, dst_table=None):
        key = (smodel, dmodel, tuple(sorted((rename or {}).items())), tuple(sorted(skip)), tuple(sorted(drop_dst_cols)))
        if key in self._plans:
            return self._plans[key]
        rename = rename or {}
        st = self.table(self.src, smodel, src_table)
        dt = self.table(self.dst, dmodel, dst_table)
        scols, dcols = self.src.columns(st), self.dst.columns(dt)
        sf, df = self.src.fields(smodel), self.dst.fields(dmodel)
        cols, dropped = [], []
        used_dc = set()

        def pick_dc(sc):
            if sc in rename:
                return rename[sc]
            if sc in dcols:
                return sc
            a = AUTO_RENAMES.get(sc)
            return a if a in dcols and a not in scols else None

        for sc, sm in scols.items():
            if sc in GLOBAL_SKIP or sc in skip:
                continue
            dc = pick_dc(sc)
            if dc is None or dc not in dcols or dc in drop_dst_cols or dc in used_dc:
                dropped.append(sc)
                continue
            dm = dcols[dc]
            smeta, dmeta = sf.get(sc, {}), df.get(dc, {})
            cd = False
            kind, rel, allowed = "plain", None, None
            if dm["fam"] == "json" and sm["fam"] != "json" and dmeta.get("translate") \
                    and dmeta.get("ttype") in ("char", "text", "html"):
                kind = "tr"
            elif dm["fam"] == "json" and sm["fam"] != "json" and dmeta.get("ttype") not in (
                    None, "json", "properties", "properties_definition", "serialized"):
                cd = True      # Odoo 20 company-dependent jsonb, source mein plain column
            if smeta.get("ttype") == "many2one" and sm["fam"] == "int":
                kind, rel = "m2o", canon(smeta.get("relation"))
                if dmeta.get("relation") and canon(dmeta["relation"]) != rel and not cd \
                        and (rel, canon(dmeta["relation"])) not in REL_OK:
                    dropped.append(sc + " (relation %s->%s)" % (rel, canon(dmeta["relation"])))
                    continue
            elif smeta.get("ttype") == "selection" or (dmeta.get("ttype") == "selection" and sm["fam"] == "text"):
                kind = "sel"
                allowed = self.dst.selection(dmodel, dc) or None
            elif sc.endswith("_id") and sm["fam"] == "int" and not smeta:
                dropped.append(sc + " (m2o meta nahi)")
                continue
            elif sm["fam"] == "json" and dm["fam"] == "json":
                kind = "json"
            elif kind != "tr" and not cd:
                if not self.compatible(sm["fam"], dm["fam"]):
                    dropped.append("%s (%s->%s)" % (sc, sm["type"], dm["type"]))
                    continue
                if dm["fam"] == "int" and sm["fam"] == "num":
                    kind = "int"
                if dm["fam"] == "num" and sm["fam"] == "int":
                    kind = "plain"
            cols.append(Col(sc=sc, dc=dc, kind=kind, rel=rel, allowed=allowed, cd=cd, nullable=dm["nullable"],
                            default=dm["default"], fam=dm["fam"], ttype=dmeta.get("ttype")))
            used_dc.add(dc)
        # create_uid / write_uid
        for u in ("create_uid", "write_uid"):
            if u in scols and u in dcols:
                cols.append(Col(sc=u, dc=u, kind="uid", rel="res.users", nullable=True, default=False, fam="int"))
                used_dc.add(u)
        # src company-dependent (ir_property) fields
        for fname in sorted(self.src_cd_fields().get(smodel, ())):
            dc = pick_dc(fname) or (fname if fname in dcols else None)
            if not dc or dc not in dcols or dc in used_dc or dc in skip or fname in skip:
                continue
            smeta, dmeta = sf.get(fname, {}), df.get(dc, {})
            dm = dcols[dc]
            cdst = dm["fam"] == "json" and not dmeta.get("translate") and dmeta.get("ttype") not in (
                None, "json", "properties", "serialized")
            cols.append(Col(sc=fname, dc=dc, kind="prop", rel=canon(smeta.get("relation")) if smeta.get("relation") else None,
                            cd=cdst, nullable=True, default=True, fam=dm["fam"], ttype=dmeta.get("ttype")))
            used_dc.add(dc)
        miss = [c for c, m in dcols.items() if not m["nullable"] and not m["default"] and c != "id" and c not in used_dc]
        p = Plan(cols, dropped, miss, st, dt, smodel, dmodel, rename)
        self._plans[key] = p
        return p

    def autofill(self, plan):
        """dst ke NOT NULL columns jin ki value source mein nahi: Odoo 20 ke apne maujooda rows ki sabse aam value.
        *_id (foreign key) columns kabhi autofill nahi hote - wahan hook chahiye."""
        out, cols = {}, self.dst.columns(plan.dst_table)
        for c in plan.missing_required:
            m = cols[c]
            if m["fam"] == "json" or c.endswith("_id") or c in ("create_date", "write_date"):
                continue
            r = self.dst.q1(sql.SQL("select {c} as v, count(*) n from {t} where {c} is not null group by 1 "
                                    "order by 2 desc limit 1").format(c=ident(c), t=ident(plan.dst_table)).as_string(self.dst.cx))
            if r:
                out[c] = r["v"]
            elif m["fam"] == "bool":
                out[c] = False
            elif m["fam"] in ("int", "num"):
                out[c] = 0
            elif m["fam"] == "text":
                out[c] = ""
        if out:
            log.info("[autofill %s] source mein nahi, dst ki aam value lagi: %s", plan.dst_table,
                     ", ".join("%s=%r" % kv for kv in out.items()))
        plan.autofill = out
        return out

    @staticmethod
    def compatible(a, b):
        return a == b or {a, b} <= {"int", "num"}

    # ------------------------------------------------------------------ conversion
    def convert(self, row, plan, strict, sel_map=None):
        vals, unres, pend = {}, [], []
        sel_map = sel_map or {}
        for c in plan.cols:
            if c.kind == "prop":
                continue
            v = row.get(c.sc)
            if c.kind == "uid":
                v = self.resolve("res.users", v) or 1
            elif c.kind == "m2o":
                if v is None:
                    pass
                else:
                    nv = self.resolve(c.rel, v)
                    if nv is None:
                        if c.sc in strict or (not c.nullable and not c.default):
                            unres.append((c.dc, c.rel, v))
                            continue
                        if c.rel in self.pending_ok and c.rel not in NEVER_MIGRATED:
                            pend.append((c.dc, c.rel, v))
                        else:
                            self.stats["dropped %s.%s -> %s" % (plan.dmodel, c.dc, c.rel)] += 1
                        v = None
                    else:
                        v = nv
            elif c.kind == "sel":
                if v is not None:
                    v = sel_map.get(c.sc, {}).get(v, v)
                    if c.allowed and v not in c.allowed:
                        self.stats["selection %s.%s=%s dst mein nahi" % (plan.dmodel, c.dc, v)] += 1
                        if not c.nullable and not c.default:
                            unres.append((c.dc, "selection", v))
                            continue
                        v = None
            elif c.kind == "tr":
                v = Json({self.base_lang: v}) if v is not None else None
            elif c.kind == "json":
                v = Json(v) if v is not None else None
            elif c.kind == "int" and v is not None:
                v = int(round(v))
            if c.cd and v is not None and c.kind != "json":
                comp = self.resolve("res.company", row.get("company_id")) if row.get("company_id") else None
                v = Json({str(comp or self.default_company()): v})
            vals[c.dc] = v
        return vals, unres, pend

    # ------------------------------------------------------------------ low level insert
    def alloc(self, table, n):
        if n <= 0:
            return []
        seq = self.dst.sequence_of(table)
        if not seq:
            raise RuntimeError("%s ki id sequence nahi mili" % table)
        with self.dst.cx.cursor() as c:
            c.execute("select nextval(%s) from generate_series(1, %s)", (seq, n))
            return [r[0] for r in c.fetchall()]

    def insert_many(self, table, rows):
        """rows: list of dict (id included). NULL ko default se badalta hai jahan NOT NULL ho."""
        if not rows:
            return
        dcols = self.dst.columns(table)
        groups = collections.defaultdict(list)
        for r in rows:
            use = {}
            for k, v in r.items():
                if v is None and k != "id":
                    m = dcols.get(k)
                    if m and not m["nullable"]:
                        if m["default"]:
                            continue            # DB default lag jayegi
                        if m["fam"] == "bool":
                            v = False
                        elif m["fam"] in ("int", "num"):
                            v = 0
                        elif m["fam"] == "text":
                            v = ""
                use[k] = v
            groups[tuple(sorted(use))].append(use)
        for sig, grp in groups.items():
            q = sql.SQL("insert into {t} ({c}) values %s").format(
                t=ident(table), c=sql.SQL(", ").join(ident(x) for x in sig)).as_string(self.dst.cx)
            self.dst.ev(q, [[g[x] for x in sig] for g in grp], 1000)

    def insert_one(self, table, vals):
        v = dict(vals)
        if "id" not in v:
            v["id"] = self.alloc(table, 1)[0]
        self.insert_many(table, [v])
        return v["id"]

    def fetch(self, table, cols, ids, order="id"):
        if not ids:
            return []
        q = sql.SQL("select {c} from {t} where id = any(%s) order by {o}").format(
            c=sql.SQL(", ").join(ident(x) for x in cols), t=ident(table), o=ident(order)).as_string(self.src.cx)
        return self.src.q(q, (list(ids),))

    # ------------------------------------------------------------------ many2many
    def m2m_fields(self, smodel, dmodel, rename, skip):
        sf, df = self.src.fields(smodel), self.dst.fields(dmodel)
        out = []
        for name, m in sf.items():
            if m["ttype"] != "many2many" or not m["store"] or name in skip:
                continue
            dn = rename.get(name) or (name if name in df else AUTO_RENAMES.get(name))
            d = df.get(dn or "")
            if not d or d["ttype"] != "many2many" or canon(d["relation"]) != canon(m["relation"]):
                continue
            if not (m["relation_table"] and d["relation_table"] and self.src.has_table(m["relation_table"])
                    and self.dst.has_table(d["relation_table"])):
                continue
            out.append((name, m, d))
        return out

    def copy_m2m(self, smodel, dmodel, pairs, rename, skip):
        """pairs: [(src_id, dst_id)] - in records ki m2m links copy karo (dono taraf map ho to)"""
        if not pairs:
            return
        sid2did = dict(pairs)
        for name, m, d in self.m2m_fields(smodel, dmodel, rename, skip):
            rows = self.src.q(sql.SQL("select {a} a, {b} b from {t} where {a} = any(%s)").format(
                t=ident(m["relation_table"]), a=ident(m["column1"]), b=ident(m["column2"])).as_string(self.src.cx),
                (list(sid2did),))
            ins = set()
            for r in rows:
                t = self.resolve(m["relation"], r["b"])
                if t is None:
                    self.stats["m2m dropped %s.%s" % (dmodel, name)] += 1
                    continue
                ins.add((sid2did[r["a"]], t))
            if ins:
                self.dst.ev(sql.SQL("insert into {t} ({a},{b}) values %s on conflict do nothing").format(
                    t=ident(d["relation_table"]), a=ident(d["column1"]), b=ident(d["column2"])).as_string(self.dst.cx),
                    sorted(ins), 2000)

    def copy_props(self, plan, pairs):
        props = [c for c in plan.cols if c.kind == "prop"]
        if not props or not pairs:
            return
        for c in props:
            data = self.load_props(plan.smodel, c.sc, [s for s, _ in pairs])
            rows = []
            for s, d in pairs:
                m = data.get(s)
                if not m:
                    continue
                if c.cd:
                    rows.append((d, Json(m)))
                else:   # Odoo 20 mein plain column: default company ki value
                    comp = str(self.default_company())
                    val = m.get(comp, next(iter(m.values())))
                    rows.append((d, val))
            if rows:
                cast = "::jsonb" if c.cd else ""
                q = sql.SQL("update {t} x set {c} = v.val" + cast + " from (values %s) as v(id, val) where x.id = v.id").format(
                    t=ident(plan.dst_table), c=ident(c.dc)).as_string(self.dst.cx)
                self.dst.ev(q, rows, 1000)

    # ------------------------------------------------------------------ results
    def add_result(self, step, model, sid, status, detail=""):
        self.results.append((step, model, sid, status, detail))

    def note_missing(self, unres, step, model, sid):
        for (_c, rel, old) in unres:
            self.missing[(rel, old)] += 1
        det = "master map nahi: " + ", ".join("%s: %s#%s" % u for u in unres[:6])
        self.add_result(step, model, sid, "SKIP", det)

    # ------------------------------------------------------------------ main document runner
    def select_ids(self, spec, st):
        where, args = ["true"], []
        if spec.where:
            where.append("(%s)" % spec.where)
        a = self.args
        if spec.date_col and (a.date_from or a.date_to):
            col, is_dt = spec.date_col
            if a.date_from:
                where.append('"%s" >= %%s' % col)
                args.append(a.date_from + (" 00:00:00" if is_dt else ""))
            if a.date_to:
                where.append('"%s" <= %%s' % col)
                args.append(a.date_to + (" 23:59:59" if is_dt else ""))
        if a.ids:
            where.append("id = any(%s)")
            args.append([int(x) for x in a.ids.split(",")])
        ids = [r["id"] for r in self.src.q('select id from "%s" where %s order by id' % (st, " and ".join(where)), args or None)]
        return ids

    def run_spec(self, spec):
        smodel, dmodel = spec.model, spec.dst_model
        st = self.table(self.src, smodel, spec.src_table)
        dt = self.table(self.dst, dmodel, spec.dst_table)
        if not self.src.has_table(st):
            log.warning("[%s] Odoo 16 mein table nahi: %s", spec.name, st)
            self.add_result(spec.name, smodel, "", "SKIP", "source table nahi")
            return
        if not self.dst.has_table(dt):
            log.warning("[%s] Odoo 20 mein table nahi (module install nahi?): %s", spec.name, dt)
            self.add_result(spec.name, smodel, "", "SKIP", "destination table nahi")
            return
        ph = self.build_plan(smodel, dmodel, spec.rename, spec.skip, src_table=st, dst_table=dt)
        pcs = {}
        for ch in spec.children:
            cst = self.table(self.src, ch.model, ch.src_table)
            cdt = self.table(self.dst, ch.dst_model, ch.dst_table)
            if not (self.src.has_table(cst) and self.dst.has_table(cdt)):
                log.warning("[%s] child table nahi: %s / %s", spec.name, cst, cdt)
                continue
            fk_dc = ch.rename.get(ch.fk) or ch.fk
            pcs[ch] = self.build_plan(ch.model, ch.dst_model, ch.rename, set(ch.skip) | {ch.fk},
                                      drop_dst_cols={fk_dc}, src_table=cst, dst_table=cdt)
            pcs[ch].fk_dc = fk_dc
            pcs[ch].missing_required = [c for c in pcs[ch].missing_required if c != fk_dc]
        self.report_plan(spec, ph, pcs)
        self.autofill(ph)
        for p in pcs.values():
            self.autofill(p)
        ids = self.select_ids(spec, st)
        done = self.idmap.done(smodel) | set(self.xmlid_map(smodel))
        todo = [i for i in ids if i not in done]
        if self.args.limit:
            todo = todo[:self.args.limit]
        log.info("== %s (%s): source %d | pehle se migrate %d | ab karna hai %d%s", spec.name, smodel, len(ids),
                 len(ids) - len(todo), len(todo), "" if self.args.apply else "  [DRY-RUN: ant mein rollback]")
        if not todo:
            return
        n_done, t0 = 0, time.time()
        for i in range(0, len(todo), spec.batch):
            chunk = todo[i:i + spec.batch]
            self.process_chunk(spec, chunk, ph, pcs)
            n_done += len(chunk)
            if (i // spec.batch) % 10 == 0 or n_done == len(todo):
                el = time.time() - t0
                log.info("[%s] %d/%d (%.0fs, %.0f docs/s)", spec.name, n_done, len(todo), el, n_done / max(el, 0.001))
        if self.args.apply:
            self.dst.cx.commit()

    def report_plan(self, spec, ph, pcs):
        log.info("[%s] %d columns copy honge", spec.name, len(ph.cols))
        if ph.dropped:
            log.info("[%s] source columns jo dst mein nahi/compatible nahi (data nahi jayega): %s", spec.name,
                     ", ".join(sorted(ph.dropped))[:600])
        if ph.missing_required:
            log.warning("[%s] dst ke NOT NULL columns jin ki value source se nahi aa rahi (hook/default chahiye): %s",
                        spec.name, ", ".join(sorted(ph.missing_required)))
        for ch, p in pcs.items():
            if p.missing_required:
                log.warning("[%s/%s] dst ke NOT NULL columns jin ki value nahi aa rahi: %s", spec.name, ch.model,
                            ", ".join(sorted(p.missing_required)))

    def process_chunk(self, spec, sids, ph, pcs):
        hcols = list(dict.fromkeys(ph.src_cols + spec.extra_read + ["id"]))
        hcols = [c for c in hcols if c in self.src.columns(ph.src_table)]
        rows = self.fetch(ph.src_table, hcols, sids)
        child_rows = {}
        for ch, p in pcs.items():
            ccols = list(dict.fromkeys(p.src_cols + ch.extra_read + ["id", ch.fk]))
            ccols = [c for c in ccols if c in self.src.columns(p.src_table)]
            q = sql.SQL("select {c} from {t} where {fk} = any(%s) order by id").format(
                c=sql.SQL(", ").join(ident(x) for x in ccols), t=ident(p.src_table), fk=ident(ch.fk)).as_string(self.src.cx)
            by = collections.defaultdict(list)
            for r in self.src.q(q, (list(sids),)):
                by[r[ch.fk]].append(r)
            child_rows[ch] = by
        self.dst.x("savepoint sp_chunk")
        try:
            out = self._insert_docs(spec, rows, child_rows, ph, pcs)
            self.idmap.flush()
            self.dst.x("release savepoint sp_chunk")
            self.idmap.accept()
            self.results.extend(out)
            if self.args.apply:
                self.dst.cx.commit()
        except Exception as e:
            self.dst.x("rollback to savepoint sp_chunk")
            self.dst.x("release savepoint sp_chunk")
            self.idmap.reject()
            if len(sids) > 1:
                log.warning("[%s] batch fail (%s) - aik aik document karke dobara", spec.name, str(e)[:160])
                for s in sids:
                    self.process_chunk(spec, [s], ph, pcs)
            else:
                log.error("[%s] %s#%s fail: %s", spec.name, spec.model, sids[0], str(e)[:300])
                self.add_result(spec.name, spec.model, sids[0], "ERROR", str(e)[:400].replace("\n", " "))

    def _insert_docs(self, spec, rows, child_rows, ph, pcs):
        out = []
        self.chunk = {}          # hooks ke liye: sirf is batch ki temporary state (rollback par khud khatam)
        hid = self.alloc(ph.dst_table, len(rows))
        for r, d in zip(rows, hid):
            self.idmap.put(spec.model, r["id"], d)
        # children ids
        for ch, p in pcs.items():
            allc = [c for r in rows for c in child_rows[ch].get(r["id"], [])]
            for c, d in zip(allc, self.alloc(p.dst_table, len(allc))):
                self.idmap.put(ch.model, c["id"], d)
        hrows, crows, ok_docs = [], collections.defaultdict(list), []
        pairs_h, pairs_c = [], collections.defaultdict(list)
        for r in rows:
            sid, did = r["id"], self.idmap.get(spec.model, r["id"])
            vals, unres, pend = self.convert(r, ph, spec.strict, spec.sel_map)
            if spec.hook:
                vals = spec.hook(r, vals, self.ctx)
                if vals is None:
                    self._drop_doc(spec, r, child_rows)
                    self.add_result(spec.name, spec.model, sid, "SKIP", "hook ne skip kiya")
                    continue
            for k, v in getattr(ph, "autofill", {}).items():
                vals.setdefault(k, v)
            cdocs, cpend = [], []
            for ch, p in pcs.items():
                for cr in child_rows[ch].get(sid, []):
                    cv, cu, cp = self.convert(cr, p, ch.strict, ch.sel_map)
                    cv[p.fk_dc] = did
                    if ch.hook:
                        cv = ch.hook(cr, cv, r, self.ctx)
                        if cv is None:
                            self.idmap.unput(ch.model, cr["id"])
                            continue
                    for k, v in getattr(p, "autofill", {}).items():
                        cv.setdefault(k, v)
                    unres += cu
                    cdocs.append((ch, p, cr, cv))
                    cpend += [(p.dst_table, self.idmap.get(ch.model, cr["id"]), a, b, c) for (a, b, c) in cp]
            if unres:
                self.note_missing(unres, spec.name, spec.model, sid)
                self._drop_doc(spec, r, child_rows)
                continue
            vals["id"] = did
            hrows.append(vals)
            pairs_h.append((sid, did))
            for (a, b, c) in pend:
                self.idmap.add_pending(ph.dst_table, did, a, b, c)
            for (t, d, a, b, c) in cpend:
                self.idmap.add_pending(t, d, a, b, c)
            for ch, p, cr, cv in cdocs:
                cv["id"] = self.idmap.get(ch.model, cr["id"])
                crows[ch].append(cv)
                pairs_c[ch].append((cr["id"], cv["id"]))
            ok_docs.append((r, did))
        self.insert_many(ph.dst_table, hrows)
        for ch, lst in crows.items():
            self.insert_many(pcs[ch].dst_table, lst)
        self.copy_m2m(spec.model, spec.dst_model, pairs_h, spec.rename, spec.m2m_skip)
        self.copy_props(ph, pairs_h)
        for ch, pr in pairs_c.items():
            self.copy_m2m(ch.model, ch.dst_model, pr, ch.rename, ())
            self.copy_props(pcs[ch], pr)
        for r, did in ok_docs:
            if spec.post:
                spec.post(r, did, self.ctx)
            out.append((spec.name, spec.model, r["id"], "OK", "dst=%s" % did))
        return out

    def _drop_doc(self, spec, r, child_rows):
        self.idmap.unput(spec.model, r["id"])
        for ch, by in child_rows.items():
            for c in by.get(r["id"], []):
                self.idmap.unput(ch.model, c["id"])

    # ------------------------------------------------------------------ parent_path rebuild
    def rebuild_parent_path(self, table):
        cols = self.dst.columns(table)
        if "parent_path" not in cols or "parent_id" not in cols:
            return 0
        rows = self.dst.q(sql.SQL("select id, parent_id from {t}").format(t=ident(table)).as_string(self.dst.cx))
        par = {r["id"]: r["parent_id"] for r in rows}
        cache, upd = {}, []

        def path(i):
            if i in cache:
                return cache[i]
            chain, seen, cur = [], set(), i
            while cur and cur not in seen and cur in par:
                seen.add(cur)
                chain.append(cur)
                cur = par[cur]
            p = "".join("%d/" % x for x in reversed(chain))
            cache[i] = p
            return p
        for i in par:
            upd.append((i, path(i)))
        self.dst.ev(sql.SQL("update {t} x set parent_path = v.p from (values %s) as v(id, p) "
                                               "where x.id = v.id").format(t=ident(table)).as_string(self.dst.cx),
                       upd, 2000)
        return len(upd)

    # ------------------------------------------------------------------ reports
    def write_reports(self, prefix="tx_db"):
        with open(prefix + "_report.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["step", "model", "src_id", "result", "detail"])
            w.writerows(self.results)
        if self.missing:
            with open("missing_masters.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["relation", "src_id", "documents_ruke"])
                for (rel, sid), n in self.missing.most_common():
                    w.writerow([rel, sid, n])
        if self.renamed:
            with open("renamed_moves.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["src_id", "dst_id", "purana_naam", "naya_naam"])
                w.writerows(self.renamed)
        summ = collections.Counter((r[0], r[3]) for r in self.results)
        log.info("---- natija ----")
        for (step, st), n in sorted(summ.items()):
            log.info("  %-18s %-8s %d", step, st, n)
        if self.stats:
            log.info("---- jo values nahi ja saki (model.field -> kitni) ----")
            for k, n in self.stats.most_common(40):
                log.info("  %-70s %d", k, n)