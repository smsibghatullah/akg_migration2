# -*- coding: utf-8 -*-
"""
Transaction specs (Odoo 16 -> Odoo 20, direct DB) + hooks + runners.
UAT par jo masle aaye the (display_type, duplicate posted names, 'done' states, POS cash method, leave types,
suspense balances ...) un ka hal yahan pehle se shamil hai.
"""
import collections
import datetime
import decimal
import logging
import re

from psycopg2 import sql
from psycopg2.extras import Json

from akg_engine import Spec, Child, canon, ident, family

log = logging.getLogger("txdb")
DUP_SUFFIX = "-DUP"


# ============================================================================ hooks
def _locked_state(ctx, table, vals, row, done_to):
    """Odoo 20 mein 'done' state nahi: sale/purchase 'done' -> confirmed + locked"""
    if row.get("state") == "done":
        vals["state"] = done_to
        if ctx.has(table, "locked"):
            vals["locked"] = True
    return vals


def pick_sel(ctx, model, field, prefs):
    """sirf unhi preferred values mein se jo dst mein sach mein hon; warna None (andaza nahi)"""
    allowed = ctx.dst.selection(model, field)
    for p in prefs:
        if p in allowed:
            return p
    return None


def _tax_mode(ctx, table, model, vals):
    if ctx.has(table, "document_tax_mode") and vals.get("document_tax_mode") is None:
        vals["document_tax_mode"] = pick_sel(ctx, model, "document_tax_mode", ["tax_excluded"])
    return vals


def hook_sale_order(row, vals, ctx):
    vals = _tax_mode(ctx, "sale_order", "sale.order", vals)
    return _locked_state(ctx, "sale_order", vals, row, "sale")


def hook_purchase_order(row, vals, ctx):
    vals = _tax_mode(ctx, "purchase_order", "purchase.order", vals)
    return _locked_state(ctx, "purchase_order", vals, row, "purchase")


def fix_analytic(row, vals, ctx):
    """analytic_distribution ki keys analytic account ids hain ('12' ya '12,15') -> dst ids"""
    v = row.get("analytic_distribution")
    if "analytic_distribution" not in vals and not v:
        return vals
    if not isinstance(v, dict) or not v:
        vals["analytic_distribution"] = None
        return vals
    new = {}
    for key, pct in v.items():
        ids = [ctx.resolve("account.analytic.account", int(x)) for x in str(key).split(",") if x.strip().isdigit()]
        if ids and all(ids):
            new[",".join(str(i) for i in ids)] = pct
    if len(new) < len(v):
        ctx.stat("analytic_distribution adhoori (analytic account map nahi)")
    vals["analytic_distribution"] = Json(new) if new else None
    return vals


_acc_types = {}


def _account_types(ctx):
    if not _acc_types:
        cols = ctx.src.columns("account_account")
        if "account_type" in cols:
            for r in ctx.src.q("select id, account_type from account_account"):
                _acc_types[r["id"]] = r["account_type"]
        else:
            _acc_types[0] = None
    return _acc_types


def hook_move_line(row, vals, parent, ctx):
    """16 mein product/tax/payment-term lines ka display_type khali; 20 mein explicit chahiye"""
    vals = fix_analytic(row, vals, ctx)
    if not ctx.has("account_move_line", "display_type"):
        return vals
    dt = row.get("display_type")
    if dt in ("line_section", "line_note"):
        val = dt
    elif row.get("tax_line_id"):
        val = "tax"
    elif parent.get("move_type") != "entry" and _account_types(ctx).get(row.get("account_id")) in (
            "asset_receivable", "liability_payable"):
        val = "payment_term"
    else:
        val = "product"
    allowed = ctx.dst.selection("account.move.line", "display_type")
    if not allowed or val in allowed:
        vals["display_type"] = val
    return vals


def hook_sale_line(row, vals, parent, ctx):
    return fix_analytic(row, vals, ctx)


def _posted_names(ctx):
    eng = ctx.eng
    if not hasattr(eng, "_posted"):
        eng._posted = {(r["name"], r["journal_id"]) for r in ctx.dst.q(
            "select name, journal_id from account_move where state = 'posted' and name is not null and name <> '/'")}
    return eng._posted


def hook_move(row, vals, ctx):
    if ctx.has("account_move", "document_tax_mode") and vals.get("document_tax_mode") is None \
            and row.get("move_type") != "entry":
        vals["document_tax_mode"] = pick_sel(ctx, "account.move", "document_tax_mode", ["tax_excluded"])
    if ctx.has("account_move", "review_state") and vals.get("review_state") is None:
        vals["review_state"] = pick_sel(ctx, "account.move", "review_state", ["no_review"])
    """Odoo 20: posted move ka naam journal mein unique. Odoo 16 mein invoice/refund ka naam same ho sakta tha."""
    if row.get("state") != "posted" or not vals.get("name") or vals["name"] == "/":
        return vals
    taken, local = _posted_names(ctx), ctx.eng.chunk.setdefault("names", set())
    key = (vals["name"], vals.get("journal_id"))
    if key in taken or key in local:
        old = vals["name"]
        for i in range(1, 100):
            cand = "%s%s%s" % (old, DUP_SUFFIX, "" if i == 1 else i)
            k2 = (cand, vals.get("journal_id"))
            if k2 not in taken and k2 not in local:
                vals["name"], key = cand, k2
                ctx.eng.renamed.append((row["id"], None, old, cand))
                break
    local.add(key)
    return vals


def post_move(row, did, ctx):
    k = ctx.dst.q1("select name, journal_id from account_move where id = %s", (did,))
    if k and row.get("state") == "posted":
        _posted_names(ctx).add((k["name"], k["journal_id"]))
    eng = ctx.eng
    for i, t in enumerate(list(eng.renamed)):
        if t[0] == row["id"] and t[1] is None:
            eng.renamed[i] = (t[0], did, t[2], t[3])


def _from_move(vals, ctx, table, fields):
    m = ctx.dst.q1("select date, journal_id, company_id, state from account_move where id = %s", (vals.get("move_id"),))
    if not m:
        return None
    for f in fields:
        if f in ctx.dst.columns(table) and vals.get(f) is None:
            vals[f] = m[f]
    return m


def hook_payment(row, vals, ctx):
    m = _from_move(vals, ctx, "account_payment", ["date", "journal_id", "company_id"])
    if m is None:
        return None
    if ctx.has("account_payment", "state") and vals.get("state") is None:
        if m["state"] == "draft":
            want = "draft"
        elif m["state"] == "cancel":
            want = "canceled"
        else:
            want = "reconciled" if row.get("is_reconciled") else "paid"
        allowed = ctx.dst.selection("account.payment", "state")
        vals["state"] = want if (not allowed or want in allowed) else pick_sel(ctx, "account.payment", "state", ["paid"])
    return vals


def hook_statement_line(row, vals, ctx):
    m = _from_move(vals, ctx, "account_bank_statement_line", ["journal_id", "company_id", "date"])
    return vals if m else None


def hook_pos_order(row, vals, ctx):
    # Odoo 20 mein config_id stored ho sakta hai -> session se
    if ctx.has("pos_order", "config_id") and not vals.get("config_id") and vals.get("session_id"):
        vals["config_id"] = ctx.dst.scalar("select config_id from pos_session where id = %s", (vals["session_id"],))
    return vals


def hook_pos_line(row, vals, parent, ctx):
    return vals


# ============================================================================ specs
SPECS = collections.OrderedDict()


def add(spec):
    SPECS[spec.name] = spec
    return spec


add(Spec("crm", "crm.lead", "CRM leads/opportunities", strict=["partner_id"], date_col=("create_date", True)))

add(Spec("sales", "sale.order", "Sale orders + lines", strict=["partner_id", "company_id"], hook=hook_sale_order,
         date_col=("date_order", True), compare=["amount_total"], batch=200,
         children=[Child("sale.order.line", "order_id", strict=["product_id", "product_uom", "product_uom_id"],
                         hook=hook_sale_line)]))

add(Spec("purchases", "purchase.order", "Purchase orders + lines", strict=["partner_id", "company_id"],
         hook=hook_purchase_order, date_col=("date_order", True), compare=["amount_total"], batch=200,
         children=[Child("purchase.order.line", "order_id", strict=["product_id", "product_uom", "product_uom_id"],
                         hook=hook_sale_line)]))

add(Spec("moves", "account.move", "Invoices, bills, journal entries (lines ke saath, EXACT copy)",
         strict=["journal_id", "partner_id", "company_id", "currency_id"], hook=hook_move, post=post_move,
         date_col=("date", False), extra_read=["move_type", "state"], compare=["amount_total", "amount_total_signed"],
         batch=300,
         children=[Child("account.move.line", "move_id", strict=["account_id", "partner_id", "product_id", "currency_id"],
                         hook=hook_move_line, skip=["payment_id", "statement_line_id", "statement_id"],
                         extra_read=["tax_line_id", "account_id", "display_type"])]))

add(Spec("payments", "account.payment", "Payments (migrated move se link)", strict=["partner_id", "journal_id", "move_id"],
         hook=hook_payment, extra_read=["is_matched", "is_reconciled"], date_col=("date", False), compare=["amount"], batch=500))

add(Spec("statement_lines", "account.bank.statement.line", "Bank statement lines (migrated move se link)",
         strict=["journal_id", "move_id"], hook=hook_statement_line, date_col=("date", False), batch=500))

add(Spec("analytic", "account.analytic.line", "Analytic lines", strict=["account_id"], date_col=("date", False), batch=1000))

add(Spec("attendance", "hr.attendance", "Attendance", strict=["employee_id"], date_col=("check_in", True), batch=1000,
         group="hr"))
add(Spec("allocations", "hr.leave.allocation", "Leave allocations", strict=["employee_id", "holiday_status_id"],
         date_col=("date_from", False), group="hr"))
add(Spec("leaves", "hr.leave", "Leaves", strict=["employee_id", "holiday_status_id"], date_col=("date_from", True),
         group="hr"))
add(Spec("expense_sheets", "hr.expense.sheet", "Expense reports", strict=["employee_id"], group="hr"))
add(Spec("expenses", "hr.expense", "Expenses", strict=["employee_id"], group="hr"))
add(Spec("payslip_runs", "hr.payslip.run", "Payslip batches", group="hr"))
# Odoo 20: contract_id -> version_id (hr.contract -> hr.version); state 'verify' dst mein nahi
add(Spec("payslips", "hr.payslip", "Payslips + lines", strict=["employee_id"], group="hr",
         rename={"contract_id": "version_id"}, sel_map={"state": {"verify": "draft"}},
         children=[Child("hr.payslip.line", "slip_id", strict=[], rename={"contract_id": "version_id"})]))

add(Spec("pos_sessions", "pos.session", "POS sessions", strict=["config_id", "user_id", "company_id"],
         date_col=("start_at", True), batch=500, group="pos",
         sel_map={"state": {}}))
add(Spec("pos_orders", "pos.order", "POS orders + lines + payments", strict=["partner_id", "user_id", "company_id",
                                                                           "session_id"],
         hook=hook_pos_order, date_col=("date_order", True), compare=["amount_total"], batch=300, group="pos",
         sel_map={"state": {"invoiced": "done"}},
         children=[Child("pos.order.line", "order_id", strict=["product_id"], hook=hook_pos_line),
                   Child("pos.payment", "pos_order_id", strict=["payment_method_id"])]))


# ============================================================================ MASTERS (direct DB)
def hook_partner(row, vals, ctx):
    """Odoo 20 mein 'mobile' nahi: phone khali ho to mobile wahan, warna notes/comment mein"""
    if row.get("mobile") and not ctx.has("res_partner", "mobile"):
        if not vals.get("phone"):
            vals["phone"] = row["mobile"]
    return vals


def hook_product_tmpl(row, vals, ctx):
    """16: detailed_type 'product' = storable. 20: type='consu' + is_storable"""
    dt = row.get("detailed_type") or row.get("type")
    if ctx.has("product_template", "is_storable") and vals.get("is_storable") is None:
        vals["is_storable"] = (dt == "product")
    if row.get("type") == "product":
        vals["type"] = "consu"
    if ctx.has("product_template", "is_favorite") and row.get("priority") is not None:
        vals["is_favorite"] = str(row["priority"]) == "1"
    return vals


def post_product_tmpl(row, did, ctx):
    """pos_categ_id -> pos_categ_ids (m2m), uom_po_id -> uom_ids (agar dst mein ye fields hon)"""
    dfields = ctx.dst.fields("product.template")
    if row.get("pos_categ_id") and "pos_categ_ids" in dfields:
        c = ctx.resolve("pos.category", row["pos_categ_id"])
        if c:
            ctx.m2m_insert("product.template", "pos_categ_ids", did, [c])
    if "uom_ids" in dfields:
        ids = {ctx.resolve("uom.uom", row.get(k)) for k in ("uom_id", "uom_po_id") if row.get(k)}
        ids.discard(None)
        ctx.m2m_insert("product.template", "uom_ids", did, sorted(ids))


def hook_variant(row, vals, ctx):
    """combination_indices source ke attribute-value ids se bana hai - dst ids alag hain -> post mein dobara"""
    if "combination_indices" in vals:
        vals["combination_indices"] = None
    return vals


def post_variant(row, did, ctx):
    if not ctx.has("product_product", "combination_indices"):
        return
    f = ctx.dst.fields("product.product").get("product_template_attribute_value_ids")
    ids = []
    if f and f.get("relation_table"):
        ids = [r["b"] for r in ctx.dst.q(sql.SQL("select {b} b from {t} where {a} = %s order by {b}").format(
            t=ident(f["relation_table"]), a=ident(f["column1"]), b=ident(f["column2"])).as_string(ctx.dst.cx), (did,))]
    ctx.dst.x("update product_product set combination_indices = %s where id = %s", (",".join(str(i) for i in ids), did))


def hook_supplierinfo(row, vals, ctx):
    """Odoo 20: uom_id zaroori -> product template ki uom"""
    if vals.get("uom_id") is None and vals.get("product_tmpl_id") and "uom_id" in ctx.dst.columns("product_supplierinfo"):
        r = ctx.dst.q1("select uom_id from product_template where id = %s", (vals["product_tmpl_id"],)) \
            if "uom_id" in ctx.dst.columns("product_template") else None
        if r and r["uom_id"]:
            vals["uom_id"] = r["uom_id"]
    return vals


def hook_uom(row, vals, ctx):
    """Odoo 20: category/factor_inv ki jagah relative_factor + relative_uom_id"""
    if ctx.has("uom_uom", "relative_factor"):
        if row.get("uom_type") == "reference":
            vals["relative_factor"] = 1.0
        else:
            f = row.get("factor_inv") or (1.0 / row["factor"] if row.get("factor") else 1.0)
            vals["relative_factor"] = float(f)
            if ctx.has("uom_uom", "relative_uom_id"):
                ref = ctx.src.q1("select id from uom_uom where category_id = %s and uom_type = 'reference' limit 1",
                                 (row.get("category_id"),))
                vals["relative_uom_id"] = ctx.resolve("uom.uom", ref["id"]) if ref else None
    return vals


M = lambda *a, **k: add(Spec(*a, group="masters", **k))   # noqa

M("m_partner_cat", "res.partner.category", "Partner tags")
M("m_partner_industry", "res.partner.industry", "Partner industries")
M("m_partner", "res.partner", "Partners (customers/vendors) + props (receivable/payable...)", hook=hook_partner,
  extra_read=["mobile"], batch=500)
M("m_uom", "uom.uom", "Units of measure", hook=hook_uom, extra_read=["uom_type", "factor", "factor_inv", "category_id"],
  where="uom_type = 'reference'")
M("m_uom2", "uom.uom", "Units of measure (non-reference)", hook=hook_uom,
  extra_read=["uom_type", "factor", "factor_inv", "category_id"], where="uom_type <> 'reference'")
M("m_prod_category", "product.category", "Product categories + props (income/expense/valuation accounts)")
M("m_pos_category", "pos.category", "POS categories")
M("m_analytic_plan", "account.analytic.plan", "Analytic plans")
M("m_analytic_account", "account.analytic.account", "Analytic accounts")
M("m_attribute", "product.attribute", "Product attributes")
M("m_attribute_value", "product.attribute.value", "Attribute values")
M("m_product_tmpl", "product.template", "Product templates + props (cost etc.)", hook=hook_product_tmpl,
  post=post_product_tmpl, extra_read=["detailed_type", "type", "pos_categ_id", "uom_id", "uom_po_id", "priority"], sel_map={"type": {"product": "consu"}}, batch=300)
M("m_ptal", "product.template.attribute.line", "Template attribute lines")
M("m_ptav", "product.template.attribute.value", "Template attribute values")
M("m_product", "product.product", "Product variants (m2m attribute combos ke saath)", hook=hook_variant,
  post=post_variant, batch=500)
M("m_supplierinfo", "product.supplierinfo", "Vendor prices", hook=hook_supplierinfo, batch=500)
M("m_pricelist", "product.pricelist", "Pricelists")
M("m_pricelist_item", "product.pricelist.item", "Pricelist rules", batch=500)
M("m_stock_location", "stock.location", "Stock locations")
M("m_orderpoint", "stock.warehouse.orderpoint", "Reorder rules")
M("m_pos_pm", "pos.payment.method", "POS payment methods")
M("m_pos_config", "pos.config", "POS configs (sirf active)", where="active")
M("m_partner_bank", "res.partner.bank", "Partner bank accounts")

MASTERS = ["m_partner_cat", "m_partner_industry", "m_partner", "m_uom", "m_uom2", "m_prod_category", "m_pos_category",
           "m_analytic_plan", "m_analytic_account", "m_attribute", "m_attribute_value", "m_product_tmpl", "m_ptal",
           "m_ptav", "m_product", "m_supplierinfo", "m_pricelist", "m_pricelist_item", "m_stock_location",
           "m_orderpoint", "m_pos_pm", "m_pos_config", "m_partner_bank"]


# ============================================================================ runners
def run_reconcile(eng):
    for sp in (Spec("reconcile_partial", "account.partial.reconcile", "partial reconcile",
                    strict=["debit_move_id", "credit_move_id"], batch=2000),
               Spec("reconcile_full", "account.full.reconcile", "full reconcile", batch=2000)):
        eng.run_spec(sp)
    n = eng.idmap.resolve_pending()
    log.info("[reconcile] pending links (lines/partials -> full reconcile, exchange moves) lage: %d", n)


def run_links(eng):
    """sale/purchase lines ke invoice links: moves ke baad dobara m2m copy (ON CONFLICT DO NOTHING)"""
    for model in ("sale.order.line", "purchase.order.line", "sale.order", "purchase.order"):
        m = eng.idmap._load(canon(model))
        pairs = list(m.items())
        for i in range(0, len(pairs), 20000):
            eng.copy_m2m(model, model, pairs[i:i + 20000], {}, ())
    n = eng.idmap.resolve_pending()
    log.info("[links] pending links lage: %d", n)


def _lot_table(db):
    for t in ("stock_lot", "stock_production_lot"):
        if db.has_table(t):
            return t
    return None


def run_stock_onhand(eng):
    """Current on-hand quantity (internal locations) seedha stock_quant mein. Inventory-adjustment ka accounting/valuation
    NAHI banta (double counting nahi) - dekho README: valuation ka note."""
    src, dst, a = eng.src, eng.dst, eng.args
    if not (src.has_table("stock_quant") and dst.has_table("stock_quant")):
        log.warning("[stock_onhand] stock_quant table nahi")
        return
    rows = src.q("select q.product_id, q.location_id, q.lot_id, q.company_id, sum(q.quantity) qty "
                 "from stock_quant q join stock_location l on l.id = q.location_id where l.usage = 'internal' "
                 "group by 1,2,3,4 having abs(sum(q.quantity)) > 0.00001 order by 1,2,3")
    if a.limit:
        rows = rows[:a.limit]
    log.info("== stock_onhand: source (product, location, lot) lines: %d", len(rows))
    slot, dlot = _lot_table(src), _lot_table(dst)
    dcols = dst.columns("stock_quant")
    ok = skip = same = 0
    lot_cache = {}

    def ensure_lot(slot_id, dprod, comp):
        k = (slot_id, dprod)
        if k in lot_cache:
            return lot_cache[k]
        r = eng.idmap.get("stock.lot", slot_id)
        if not r and slot and dlot:
            s = src.q1(sql.SQL("select name, ref, company_id from {t} where id = %s").format(t=ident(slot)).as_string(src.cx),
                       (slot_id,))
            if s:
                f = dst.q1(sql.SQL("select id from {t} where name = %s and product_id = %s limit 1").format(
                    t=ident(dlot)).as_string(dst.cx), (s["name"], dprod))
                if f:
                    r = f["id"]
                else:
                    vals = {"name": s["name"], "product_id": dprod, "company_id": comp}
                    if s.get("ref") and "ref" in dst.columns(dlot):
                        vals["ref"] = s["ref"]
                    r = eng.insert_one(dlot, vals)
                eng.idmap.set_direct("stock.lot", slot_id, r, created=not f)
        lot_cache[k] = r
        return r

    for x in rows:
        dp = eng.resolve("product.product", x["product_id"])
        dl = eng.resolve("stock.location", x["location_id"])
        if not dp or not dl:
            skip += 1
            eng.add_result("stock_onhand", "stock.quant", x["product_id"], "SKIP",
                           "map nahi: product=%s location=%s" % (dp, dl))
            if not dp:
                eng.missing[("product.product", x["product_id"])] += 1
            if not dl:
                eng.missing[("stock.location", x["location_id"])] += 1
            continue
        comp = eng.resolve("res.company", x["company_id"]) if x["company_id"] else None
        comp = comp or eng.default_company()
        lot = ensure_lot(x["lot_id"], dp, comp) if x["lot_id"] else None
        cur = dst.scalar("select coalesce(sum(quantity),0) from stock_quant where product_id = %s and location_id = %s "
                         "and lot_id is not distinct from %s", (dp, dl, lot))
        delta = float(x["qty"]) - float(cur or 0)
        if abs(delta) < 0.00001:
            same += 1
            continue
        vals = {"product_id": dp, "location_id": dl, "lot_id": lot, "company_id": comp, "quantity": delta,
                "reserved_quantity": 0.0}
        if "in_date" in dcols:
            vals["in_date"] = datetime.datetime.utcnow()
        if "create_uid" in dcols:
            vals["create_uid"] = 1
            vals["write_uid"] = 1
        eng.insert_one("stock_quant", vals)
        ok += 1
        eng.add_result("stock_onhand", "stock.quant", x["product_id"], "OK", "qty %+.3f loc=%s" % (delta, dl))
    if a.apply:
        dst.cx.commit()
    log.info("[stock_onhand] insert %d | pehle se barabar %d | skip %d", ok, same, skip)


def run_leave_types(eng):
    """Odoo 20 mein hr.leave.type nahi, leave types = hr.work.entry.type. Jo source leave types dst mein map nahi unhe
    ek maujooda leave work-entry-type ki COPY bana kar (NOT NULL/selection columns sahi rahein) naam/code lagao."""
    src, dst = eng.src, eng.dst
    if not (src.has_table("hr_leave_type") and dst.has_table("hr_work_entry_type")):
        log.warning("[leave_types] table nahi (source hr_leave_type / dst hr_work_entry_type)")
        return
    dcols = dst.columns("hr_work_entry_type")
    done = eng.idmap.done("hr.leave.type") | set(eng.xmlid_map("hr.leave.type"))
    todo = [r for r in src.q("select * from hr_leave_type order by id") if r["id"] not in done]
    log.info("== leave_types: source %d | map nahi, banane hain: %d", len(src.q("select id from hr_leave_type")), len(todo))
    if not todo:
        return
    tpl = dst.q1("select * from hr_work_entry_type where is_leave order by id limit 1") if "is_leave" in dcols \
        else dst.q1("select * from hr_work_entry_type order by id limit 1")
    if not tpl:
        log.error("[leave_types] dst mein template (koi bhi work entry type) nahi mila")
        return
    for r in todo:
        vals = {k: v for k, v in tpl.items() if k not in ("id", "create_date", "write_date")}
        for k, v in list(vals.items()):
            if isinstance(v, (dict, list)):
                vals[k] = Json(v)
        nm = r.get("name")
        if dcols["name"]["fam"] == "json":
            vals["name"] = Json(nm if isinstance(nm, dict) else {eng.base_lang: nm})
        else:
            vals["name"] = nm.get(eng.base_lang) if isinstance(nm, dict) else nm
        if "code" in dcols:
            vals["code"] = "OMLT%d" % r["id"]
        if "external_code" in vals:
            vals["external_code"] = None
        if "is_leave" in dcols:
            vals["is_leave"] = True
        if "active" in dcols:
            vals["active"] = True
        if "create_uid" in dcols:
            vals["create_uid"] = vals["write_uid"] = 1
        did = eng.insert_one("hr_work_entry_type", vals)
        eng.idmap.set_direct("hr.leave.type", r["id"], did, created=True)
        eng.add_result("leave_types", "hr.leave.type", r["id"], "OK", "dst work entry type=%s" % did)
        log.info("[leave_types] #%s %s -> work entry type %s (code OMLT%d)", r["id"], nm if not isinstance(nm, dict) else nm.get(eng.base_lang), did, r["id"])
    if eng.args.apply:
        dst.cx.commit()


SEQ_CODES = ("sale.order", "purchase.order", "pos.session", "crm.lead")


def run_sequences(eng):
    """Naye documents ka naam purane migrated naamon se na takraye: source ki sequence ki agli value dst mein lagao
    (sirf barhata hai, kam kabhi nahi). account.move ko zaroorat nahi (journal se last name dekhta hai)."""
    src, dst = eng.src, eng.dst
    if not (src.has_table("ir_sequence") and dst.has_table("ir_sequence")):
        return
    for code in SEQ_CODES:
        for s in src.q("select * from ir_sequence where code = %s", (code,)):
            comp = eng.resolve("res.company", s["company_id"]) if s.get("company_id") else None
            if s.get("implementation") == "no_gap":
                nxt, inc = s["number_next"], s["number_increment"]
            else:
                seq = "ir_sequence_%03d" % s["id"]
                try:
                    r = src.q1(sql.SQL("select last_value, is_called from {t}").format(t=ident(seq)).as_string(src.cx))
                except Exception:   # noqa
                    log.warning("[sequences] %s ki postgres sequence nahi mili", seq)
                    continue
                inc = s["number_increment"]
                nxt = r["last_value"] + (inc if r["is_called"] else 0)
            cand = dst.q("select * from ir_sequence where code = %s and company_id is not distinct from %s order by id",
                         (code, comp))
            if not cand:
                log.warning("[sequences] dst mein code=%s company=%s ki sequence nahi", code, comp)
                continue
            d = cand[0]
            if d.get("implementation") == "no_gap":
                dst.x("update ir_sequence set number_next = greatest(number_next, %s) where id = %s", (nxt, d["id"]))
            else:
                seq = "ir_sequence_%03d" % d["id"]
                cur = dst.q1(sql.SQL("select last_value, is_called from {t}").format(t=ident(seq)).as_string(dst.cx))
                cur_next = cur["last_value"] + (d["number_increment"] if cur["is_called"] else 0)
                if nxt > cur_next:
                    dst.scalar("select setval(%s, %s, false)", (seq, nxt))
            log.info("[sequences] %s company=%s: source next=%s -> dst set", code, comp, nxt)
    if eng.args.apply:
        dst.cx.commit()


def run_parent_paths(eng):
    for t in ("stock_location", "product_category", "pos_category", "hr_department", "res_partner_category",
              "account_group"):
        if eng.dst.has_table(t):
            n = eng.rebuild_parent_path(t)
            if n:
                log.info("[parent_path] %s: %d rows", t, n)


add(Spec("leave_types", "hr.leave.type", "Leave types -> work entry types (jo dst mein nahi)", runner=run_leave_types,
         group="hr"))
add(Spec("reconcile", "account.partial.reconcile", "Reconciliation (partial + full)", runner=run_reconcile))
add(Spec("links", "sale.order", "sale/purchase <-> invoice links (moves ke baad)", runner=run_links))
add(Spec("stock_onhand", "stock.quant", "Stock on-hand quantity (internal locations)", runner=run_stock_onhand,
         group="stock"))
add(Spec("sequences", "ir.sequence", "Sequences (naye documents ke liye)", runner=run_sequences, group="final"))

CORE = ["crm", "sales", "purchases", "moves", "payments", "statement_lines", "reconcile", "links", "analytic",
        "pos_sessions", "pos_orders", "stock_onhand", "sequences"]
GROUPS = {"core": CORE, "masters": MASTERS,
          "hr": ["leave_types", "attendance", "allocations", "leaves", "expense_sheets", "expenses", "payslip_runs", "payslips"],
          "pos": ["pos_sessions", "pos_orders"],
          "all": MASTERS + CORE + ["leave_types", "attendance", "allocations", "leaves", "expense_sheets", "expenses", "payslip_runs",
                         "payslips"]}
ORDER = MASTERS + [n for n in SPECS if n not in MASTERS]
ORDER.remove("leave_types")
ORDER.insert(ORDER.index("attendance"), "leave_types")


def models_in_specs():
    out = set()
    for sp in SPECS.values():
        out.add(canon(sp.model))
        for c in sp.children:
            out.add(canon(c.model))
    out |= {"account.partial.reconcile", "account.full.reconcile", "stock.lot"}
    return out


# ============================================================================ master mapping (check / --fix)
KEYS = {
    "res.company": [("name",)], "res.currency": [("name",)], "res.country": [("code",)],
    "res.country.state": [("country_id", "code")], "res.partner.category": [("name",)],
    "res.partner": [("ref",), ("vat", "name"), ("email", "name"), ("phone", "name"), ("name",)],
    "res.users": [("login",)], "hr.employee": [("work_email", "name"), ("name",)],
    "product.category": [("complete_name",), ("name", "parent_id")], "uom.uom": [("name",)],
    "product.template": [("default_code", "name"), ("name",)],
    "product.product": [("default_code",), ("barcode",), ("name",)],
    "product.pricelist": [("name",)], "account.account": [("code",)], "account.journal": [("code",)],
    "account.tax": [("name", "type_tax_use")], "account.payment.term": [("name",)],
    "account.fiscal.position": [("name",)], "account.analytic.account": [("name",)],
    "account.payment.method.line": [("journal_id", "payment_method_id", "name")],
    "stock.warehouse": [("code",)], "stock.warehouse.orderpoint": [("product_id", "location_id")], "stock.location": [("complete_name",), ("barcode",)], "crm.stage": [("name",)],
    "crm.team": [("name",)], "crm.lost.reason": [("name",)], "utm.source": [("name",)],
    "utm.medium": [("name",)], "utm.campaign": [("name",)], "hr.leave.type": [("name",)],
    "pos.config": [("name",)], "pos.payment.method": [("name",)], "pos.category": [("name",)],
    "hr.work.entry.type": [("code",)], "hr.payroll.structure.type": [("name",)],
    "hr.payroll.structure": [("code",), ("name",)], "hr.salary.rule.category": [("code",)],
    "hr.salary.rule": [("code", "struct_id")], "approval.category": [("name",)],
}
DEPS = list(KEYS)
MODEL_TABLE = {"hr.leave.type": "hr_leave_type"}


def _norm(x):
    return re.sub(r"\s+", " ", x.strip()).lower() if isinstance(x, str) else x


def _key_expr(db, model, table, col):
    cols = db.columns(table)
    if col not in cols and (col + "_store") in cols:      # Odoo 18+: account.code -> code_store jsonb
        return sql.SQL("(select value from jsonb_each_text({c}) limit 1)").format(c=ident(col + "_store"))
    meta = db.fields(model).get(col, {})
    if db.columns(table).get(col, {}).get("fam") == "json" and meta.get("translate"):
        return sql.SQL("coalesce({c}->>%s, (select value from jsonb_each_text({c}) limit 1))").format(c=ident(col))
    return ident(col)


def _fetch_keys(db, model, table, flds, base_lang):
    parts, args = [sql.SQL("id")], []
    for f in flds:
        e = _key_expr(db, model, table, f)
        parts.append(sql.SQL("{} as {}").format(e, ident(f)))
        if "coalesce" in e.as_string(db.cx):
            args.append(base_lang)
    q = sql.SQL("select {} from {}").format(sql.SQL(", ").join(parts), ident(table)).as_string(db.cx)
    return db.q(q, args or None)


def fix_model(eng, model):
    src, dst = eng.src, eng.dst
    st, dt = MODEL_TABLE.get(model, model.replace(".", "_")), model.replace(".", "_")
    if not (src.has_table(st) and dst.has_table(dt)):
        return 0
    sc, dc = src.columns(st), dst.columns(dt)
    keys = [k for k in KEYS.get(model, []) if all(f in sc and (f in dc or (f + "_store") in dc) for f in k)]
    if not keys:
        return 0
    done = eng.idmap.done(model) | set(eng.xmlid_map(model))
    flds = sorted({f for k in keys for f in k})
    srows = [r for r in _fetch_keys(src, model, st, flds, eng.base_lang) if r["id"] not in done]
    if not srows:
        return 0
    drows = _fetch_keys(dst, model, dt, flds, eng.base_lang)
    sf = src.fields(model)

    def kv(r, k, side):
        out = []
        for f in k:
            v = r.get(f)
            if sf.get(f, {}).get("ttype") == "many2one" and v:
                v = eng.resolve(sf[f]["relation"], v) if side == "src" else v
            v = _norm(v) if isinstance(v, str) else v
            if isinstance(v, (int, float, decimal.Decimal)) and not isinstance(v, bool) \
                    and sf.get(f, {}).get("ttype") != "many2one":
                v = round(float(v), 4)
            if sf.get(f, {}).get("ttype") == "many2one" and not v:
                out.append(0)            # khali parent (root) bhi key ka hissa hai
                continue
            if v in (False, None, "", 0):
                return None
            out.append(v)
        return tuple(out) if any(out) else None
    index = {k: collections.defaultdict(list) for k in keys}
    for r in drows:
        for k in keys:
            v = kv(r, k, "dst")
            if v:
                index[k][v].append(r["id"])
    claimed = eng.idmap.dst_ids(model)
    n = 0
    left = []
    for r in srows:
        for k in keys:
            v = kv(r, k, "src")
            if not v:
                continue
            cand = [i for i in index[k].get(v, []) if i not in claimed]
            if len(cand) == 1:
                eng.idmap.set_direct(model, r["id"], cand[0], created=False)
                claimed.add(cand[0])
                n += 1
                break
        else:
            left.append(r)
    # duplicates: source mein 2 (ya zyada) records ki key same aur dst mein bhi utne hi -> id ki tarteeb se jorho
    for k in keys:
        grp = collections.defaultdict(list)
        for r in left:
            if eng.idmap.get(model, r["id"]) is None:
                v = kv(r, k, "src")
                if v:
                    grp[v].append(r)
        for v, rs in grp.items():
            cand = sorted(i for i in index[k].get(v, []) if i not in claimed)
            if len(cand) == len(rs):
                for r, d in zip(rs, cand):
                    eng.idmap.set_direct(model, r["id"], d, created=False)
                    claimed.add(d)
                    n += 1
    return n


def fix_variants(eng):
    src, dst = eng.src, eng.dst
    if not (src.has_table("product_product") and dst.has_table("product_product")):
        return 0
    done = eng.idmap.done("product.product") | set(eng.xmlid_map("product.product"))
    by_t = collections.defaultdict(list)
    for r in src.q("select id, product_tmpl_id, default_code from product_product order by id"):
        if r["id"] in done:
            continue
        t = eng.resolve("product.template", r["product_tmpl_id"])
        if t:
            by_t[t].append(r)
    claimed = eng.idmap.dst_ids("product.product")
    n = 0
    tids = list(by_t)
    for j in range(0, len(tids), 2000):
        chunk = tids[j:j + 2000]
        dby = collections.defaultdict(list)
        for d in dst.q("select id, product_tmpl_id, default_code from product_product where product_tmpl_id = any(%s) "
                       "order by id", (chunk,)):
            if d["id"] not in claimed:
                dby[d["product_tmpl_id"]].append(d)
        for t in chunk:
            ss, dd = by_t[t], dby.get(t, [])
            if len(ss) == 1 and len(dd) == 1:
                pairs = [(ss[0], dd[0])]
            else:
                dc = {d["default_code"]: d for d in dd if d["default_code"]}
                pairs = [(s, dc[s["default_code"]]) for s in ss if s["default_code"] in dc]
            for s, d in pairs:
                eng.idmap.set_direct("product.product", s["id"], d["id"], created=False)
                claimed.add(d["id"])
                n += 1
    return n


def fix_repartition(eng):
    """Tax repartition lines: 16 ki lines ko dst ki lines se map karo, accounts/tags copy"""
    src, dst = eng.src, eng.dst
    t = "account_tax_repartition_line"
    if not (src.has_table(t) and dst.has_table(t)):
        return 0
    sc, dc = src.columns(t), dst.columns(t)
    new_style = "document_type" in dc
    n = 0
    for tx in src.q("select id from account_tax"):
        st = tx["id"]
        dtx = eng.resolve("account.tax", st)
        if not dtx:
            continue
        for doc in ("invoice", "refund"):
            scol = "%s_tax_id" % doc if "%s_tax_id" % doc in sc else "tax_id"
            srows = src.q(sql.SQL("select * from {t} where {c} = %s order by sequence, id").format(
                t=ident(t), c=ident(scol)).as_string(src.cx), (st,))
            if new_style:
                drows = dst.q("select * from account_tax_repartition_line where tax_id = %s and document_type = %s "
                              "order by sequence, id", (dtx, doc))
            else:
                drows = dst.q(sql.SQL("select * from {t} where {c} = %s order by sequence, id").format(
                    t=ident(t), c=ident("%s_tax_id" % doc)).as_string(dst.cx), (dtx,))
            used = set()
            for sr in srows:
                m = next((d for d in drows if d["id"] not in used and d["repartition_type"] == sr["repartition_type"]
                          and abs(float(d["factor_percent"] or 0) - float(sr["factor_percent"] or 0)) < 0.001), None)
                acc = eng.resolve("account.account", sr.get("account_id")) if sr.get("account_id") else None
                if m:
                    used.add(m["id"])
                    if acc:
                        dst.x("update account_tax_repartition_line set account_id = %s where id = %s", (acc, m["id"]))
                    eng.idmap.set_direct("account.tax.repartition.line", sr["id"], m["id"], created=False)
                    n += 1
    return n


def cmd_check(eng, fix):
    print("\n%-30s %8s %8s %8s %s" % ("model", "source", "mapped", "missing", "note"))
    rows = []
    for model in DEPS:
        st, dt = MODEL_TABLE.get(model, model.replace(".", "_")), model.replace(".", "_")
        if not eng.src.has_table(st) or not eng.dst.has_table(dt):
            print("%-30s  (table nahi: %s)" % (model, "source" if not eng.src.has_table(st) else "destination"))
            continue
        fixed = 0
        if fix:
            for _ in range(8):           # parent pehle map hon to bachon ki key resolve hoti hai
                n = fix_model(eng, model)
                fixed += n
                if not n:
                    break
        total = eng.src.scalar(sql.SQL("select count(*) from {t}").format(t=ident(st)).as_string(eng.src.cx))
        done = eng.idmap.done(model) | set(eng.xmlid_map(model))
        ids = {r["id"] for r in eng.src.q(sql.SQL("select id from {t}").format(t=ident(st)).as_string(eng.src.cx))}
        missing = len(ids - done)
        print("%-30s %8d %8d %8d %s" % (model, total, len(ids & done), missing,
                                        ("(abhi %d map kiye)" % fixed) if fixed else ""))
        rows.append((model, total, len(ids & done), missing))
    if fix:
        v = fix_variants(eng)
        r = fix_repartition(eng)
        print("\nvariants map: %d | tax repartition lines map: %d" % (v, r))
    return rows


# ============================================================================ idmap sanity (UAT idmap PROD par na lag jaye)
SANITY = [("res.partner", "name"), ("account.account", "code"), ("account.journal", "code"), ("account.tax", "name"),
          ("product.product", "default_code"), ("uom.uom", "name"), ("product.category", "complete_name"),
          ("stock.location", "complete_name"), ("res.users", "login"), ("pos.config", "name")]


def sanity_idmap(eng):
    bad = []
    for model, col in SANITY:
        t = model.replace(".", "_")
        if not (eng.src.has_table(t) and eng.dst.has_table(t)) or col not in eng.src.columns(t) or col not in eng.dst.columns(t):
            continue
        m = eng.idmap._load(canon(model))
        if not m:
            continue
        sample = list(m.items())[:40]
        s = {r["id"]: r[col] for r in _fetch_keys(eng.src, model, t, [col], eng.base_lang) if r["id"] in dict(sample)} \
            if len(m) < 3000 else {}
        if not s:
            s = {r["id"]: r[col] for r in eng.src.q(sql.SQL("select id, {c} from {t} where id = any(%s)").format(
                c=ident(col), t=ident(t)).as_string(eng.src.cx), ([a for a, _ in sample],))}
        d = {r["id"]: r[col] for r in _fetch_keys(eng.dst, model, t, [col], eng.base_lang) if r["id"] in {b for _, b in sample}} \
            if len(m) < 3000 else {r["id"]: r[col] for r in eng.dst.q(
                sql.SQL("select id, {c} from {t} where id = any(%s)").format(c=ident(col), t=ident(t)).as_string(eng.dst.cx),
                ([b for _, b in sample],))}
        tot = mism = missing_dst = 0
        for a, b in sample:
            if a not in s:
                continue
            tot += 1
            if b not in d:
                missing_dst += 1
            elif _norm(s[a]) != _norm(d[b]) and not (s[a] is None or d[b] is None):
                mism += 1
        if tot and (mism + missing_dst) / tot > 0.4:
            bad.append("%s.%s: %d/%d sample galat (dst mein id nahi: %d, naam alag: %d)" % (model, col, mism + missing_dst, tot,
                                                                                         missing_dst, mism))
    return bad


# ============================================================================ verify
def cmd_verify(eng, steps):
    import csv
    src, dst = eng.src, eng.dst
    rows = []
    print("\n%-16s %-26s %9s %9s %9s | %s" % ("step", "model", "src", "migrated", "dst(mapped)", "sums (src vs dst, sirf migrated)"))
    for name in steps:
        sp = SPECS[name]
        if sp.runner:
            continue
        st, dt = sp.model.replace(".", "_"), sp.dst_model.replace(".", "_")
        if not (src.has_table(st) and dst.has_table(dt)):
            continue
        total = src.scalar(sql.SQL("select count(*) from {t}").format(t=ident(st)).as_string(src.cx))
        m = eng.idmap._load(canon(sp.model))
        created = dst.q("select src_id, dst_id from akg_idmap where model = %s and created", (canon(sp.model),))
        did = [r["dst_id"] for r in created]
        sid = [r["src_id"] for r in created]
        dcount = dst.scalar(sql.SQL("select count(*) from {t} where id = any(%s)").format(t=ident(dt)).as_string(dst.cx), (did,)) if did else 0
        sums = []
        for col in sp.compare:
            if col not in src.columns(st) or col not in dst.columns(dt):
                continue
            ss = 0.0
            for i in range(0, len(sid), 20000):
                ss += float(src.scalar(sql.SQL("select coalesce(sum({c}),0) from {t} where id = any(%s)").format(
                    c=ident(col), t=ident(st)).as_string(src.cx), (sid[i:i + 20000],)) or 0)
            ds = 0.0
            for i in range(0, len(did), 20000):
                ds += float(dst.scalar(sql.SQL("select coalesce(sum({c}),0) from {t} where id = any(%s)").format(
                    c=ident(col), t=ident(dt)).as_string(dst.cx), (did[i:i + 20000],)) or 0)
            flag = "OK" if abs(ss - ds) < 0.05 else "FARQ %.2f" % (ds - ss)
            sums.append("%s %.2f vs %.2f %s" % (col, ss, ds, flag))
        print("%-16s %-26s %9d %9d %9d | %s" % (name, sp.model, total, len(sid), dcount, "; ".join(sums)))
        rows.append((name, sp.model, total, len(sid), dcount, "; ".join(sums)))
    # trial balance (posted moves): account-wise debit/credit
    if "moves" in steps and src.has_table("account_move_line") and dst.has_table("account_move_line"):
        s = src.q("select l.account_id, sum(l.debit) d, sum(l.credit) c from account_move_line l join account_move m on m.id = l.move_id "
                  "where m.state = 'posted' group by 1")
        d = {r["account_id"]: r for r in dst.q("select l.account_id, sum(l.debit) d, sum(l.credit) c from account_move_line l "
                                                "join account_move m on m.id = l.move_id where m.state = 'posted' group by 1")}
        bad = 0
        out = []
        for r in s:
            da = eng.resolve("account.account", r["account_id"])
            x = d.get(da)
            dd, dc = (float(x["d"] or 0), float(x["c"] or 0)) if x else (0.0, 0.0)
            diff = abs(float(r["d"] or 0) - dd) + abs(float(r["c"] or 0) - dc)
            out.append((r["account_id"], da, float(r["d"] or 0), float(r["c"] or 0), dd, dc, round(diff, 3)))
            if diff > 0.05:
                bad += 1
        with open("tb_compare_db.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["src_account", "dst_account", "src_debit", "src_credit", "dst_debit", "dst_credit", "diff"])
            w.writerows(out)
        print("\nTrial balance (posted): %d accounts, %d mein farq -> tb_compare_db.csv" % (len(out), bad))
    with open("tx_db_verify.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["step", "model", "src_total", "migrated", "dst_count", "sums"])
        w.writerows(rows)


# ============================================================================ rollback (sirf is tool ke BANAYE hue rows)
def cmd_rollback(eng, steps):
    dst = eng.dst
    for name in reversed(steps):
        sp = SPECS[name]
        if name == "leave_types":
            n = dst.x("delete from hr_work_entry_type where id in (select dst_id from akg_idmap where model = 'hr.leave.type' and created)")
            dst.x("delete from akg_idmap where model = 'hr.leave.type' and created")
            log.info("[rollback] hr_work_entry_type: %d rows delete", n)
            continue
        if name == "reconcile":
            models = [("account.full.reconcile",) * 2, ("account.partial.reconcile",) * 2]
        elif sp.runner:
            continue
        else:
            models = [(c.model, c.dst_model) for c in reversed(sp.children)] + [(sp.model, sp.dst_model)]
        for sm, dm in models:
            dt = dm.replace(".", "_")
            n = dst.x(sql.SQL("delete from {t} where id in (select dst_id from akg_idmap where model = %s and created)").format(
                t=ident(dt)).as_string(dst.cx), (canon(sm),))
            dst.x("delete from akg_idmap where model = %s and created", (canon(sm),))
            dst.x("delete from akg_pending where dst_table = %s", (dt,))
            log.info("[rollback] %s: %d rows delete", dm, n)


PREFLIGHT = ["account.account", "account.journal", "account.tax", "res.partner", "product.product", "uom.uom"]


def preflight(eng):
    """--apply se pehle: zaroori masters map hone chahiye, warna documents skip/adhoore columns ke saath jayenge"""
    bad = []
    for model in PREFLIGHT:
        t = model.replace(".", "_")
        if not (eng.src.has_table(t) and eng.dst.has_table(t)):
            continue
        ids = {r["id"] for r in eng.src.q(sql.SQL("select id from {t}").format(t=ident(t)).as_string(eng.src.cx))}
        done = eng.idmap.done(model) | set(eng.xmlid_map(model))
        pct = 100.0 * len(ids & done) / max(len(ids), 1)
        if pct < 95:
            bad.append("%s: sirf %.0f%% map (%d/%d)" % (model, pct, len(ids & done), len(ids)))
    return bad