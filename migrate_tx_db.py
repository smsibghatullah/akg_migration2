#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Odoo 16 -> Odoo 20  TRANSACTION migration, DIRECT PostgreSQL (XML-RPC / JSON-2 nahi).

  python3 migrate_tx_db.py gaps                          # sirf PADHTA hai: har model ke columns ka match/gap report
  python3 migrate_tx_db.py check [--fix]                 # masters ka map (idmap.json + natural keys)
  python3 migrate_tx_db.py run --steps core --limit 5    # DRY-RUN: sab kuch chalta hai, aakhir mein ROLLBACK
  python3 migrate_tx_db.py run --steps core --apply --confirm-db <DST_DB_NAAM>
  python3 migrate_tx_db.py run --steps moves --apply --confirm-db <DB> --date-from 2025-01-01
  python3 migrate_tx_db.py verify --steps core
  python3 migrate_tx_db.py rollback --steps moves --apply --confirm-db <DB>     # sirf is tool ke banaye rows
  python3 migrate_tx_db.py export-idmap --out idmap_after_tx.json
  python3 migrate_tx_db.py recompute-script              # Odoo shell script: v20 ke naye stored-computed fields

Steps:  python3 migrate_tx_db.py list
"""
import argparse
import logging
import os
import sys
import time

import config
from akg_engine import DB, Engine, canon
import akg_tx_specs as S

log = logging.getLogger("txdb")


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("tx_db_migration.log", encoding="utf-8")])


def parse():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "gaps", "check", "run", "verify", "rollback", "export-idmap", "recompute-script"])
    ap.add_argument("--steps", default="core", help="core | all | hr | pos | comma list (python3 migrate_tx_db.py list)")
    ap.add_argument("--apply", action="store_true", help="asal mein commit karo (warna sab kuch aakhir mein rollback)")
    ap.add_argument("--confirm-db", default="", help="--apply ke saath DST database ka naam dobara likho (galti se UAT/PROD mix na ho)")
    ap.add_argument("--fix", action="store_true", help="check: natural keys se masters map karo")
    ap.add_argument("--limit", type=int, default=0, help="har step mein sirf itne documents (test)")
    ap.add_argument("--ids", default="", help="sirf ye source ids (comma)")
    ap.add_argument("--date-from", default="")
    ap.add_argument("--date-to", default="")
    ap.add_argument("--idmap", default=config.MASTER_IDMAP, help="master script ka idmap.json (PROD wala!)")
    ap.add_argument("--no-idmap-import", action="store_true")
    ap.add_argument("--base-lang", default=config.BASE_LANG)
    ap.add_argument("--force-version", action="store_true", help="Odoo version check ignore karo")
    ap.add_argument("--force-masters", action="store_true")
    ap.add_argument("--force-idmap", action="store_true", help="idmap sanity check ignore karo")
    ap.add_argument("--out", default="idmap_after_tx.json")
    return ap.parse_args()


def resolve_steps(spec):
    names = []
    for part in spec.split(","):
        part = part.strip()
        if part in S.GROUPS:
            names += S.GROUPS[part]
        elif part in S.SPECS:
            names.append(part)
        elif part:
            sys.exit("Unknown step: %s   (python3 migrate_tx_db.py list)" % part)
    seen, out = set(), []
    for n in S.ORDER:         # hamesha sahi tarteeb
        if n in names and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def connect(args, write):
    if not config.DST.get("dbname"):
        sys.exit("DST_PGDATABASE set nahi (config.py dekho)")
    src = DB(config.SRC, "SRC", readonly=True)
    dst = DB(config.DST, "DST", readonly=not write)
    vs, vd = src.version(), dst.version()
    log.info("SOURCE: %s@%s/%s  (Odoo %s)", config.SRC["user"], config.SRC["host"], config.SRC["dbname"], vs)
    log.info("DEST  : %s@%s/%s  (Odoo %s)", config.DST["user"], config.DST["host"], config.DST["dbname"], vd)
    if (config.SRC["host"], config.SRC["port"], config.SRC["dbname"]) == (config.DST["host"], config.DST["port"], config.DST["dbname"]):
        sys.exit("SOURCE aur DEST ek hi database hain - ruk gaya.")
    if not args.force_version and not (vs.startswith("16.") and vd.split(".")[0].isdigit() and int(vd.split(".")[0]) >= 19):
        sys.exit("Version check: source 16.x aur dest 19+/20 hona chahiye (mila: %s -> %s). --force-version se bypass." % (vs, vd))
    return src, dst


def guard_apply(args):
    if args.apply and args.confirm_db != config.DST["dbname"]:
        sys.exit("--apply ke liye --confirm-db %s likhna zaroori hai (PROD par galti se na chale)" % config.DST["dbname"])


def warn_active_sessions(dst):
    try:
        r = dst.q("select count(*) n from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid() "
                  "and application_name not like 'akg_%%'")
        if r and r[0]["n"]:
            log.warning("DEST par %d aur connections active hain (Odoo server?). PROD par migration ke waqt Odoo BAND rakho.", r[0]["n"])
    except Exception:   # noqa
        pass


def main():
    args = parse()
    setup_logging()
    if args.cmd == "list":
        print("%-16s %-8s %s" % ("step", "group", "description"))
        for n in S.ORDER:
            sp = S.SPECS[n]
            print("%-16s %-8s %s%s" % (n, sp.group, sp.desc, "   [core]" if n in S.CORE else ""))
        print("\ngroups: masters | core | hr | pos | all")
        return
    write = args.cmd in ("check", "run", "rollback") and (args.apply or args.cmd == "run" or (args.cmd == "check" and args.fix))
    if args.cmd in ("run", "rollback") or (args.cmd == "check" and args.fix):
        guard_apply(args) if args.apply else None
    src, dst = connect(args, write=write or args.cmd in ("run", "verify", "export-idmap", "check", "rollback"))
    eng = Engine(src, dst, args, create_tables=(args.cmd in ("run", "check", "rollback", "export-idmap", "verify")))
    eng.pending_ok = S.models_in_specs()
    t0 = time.time()
    try:
        if args.cmd in ("run", "check", "verify", "rollback", "export-idmap") and not args.no_idmap_import:
            n = eng.idmap.import_json(args.idmap)
            log.info("master idmap import: %d entries (%s)", n, args.idmap)
            if args.cmd in ("run", "check") and not args.force_idmap:
                bad = S.sanity_idmap(eng)
                if bad:
                    for b in bad:
                        log.error("IDMAP SANITY: %s", b)
                    sys.exit("idmap.json is DB ke masters se match nahi karta (kya ye UAT ka idmap hai?). "
                             "PROD ke liye masters PROD par migrate karke naya idmap.json use karo. (--force-idmap se bypass)")
        if args.cmd == "gaps":
            return cmd_gaps(eng, resolve_steps(args.steps if args.steps != "core" else "all"))
        if args.cmd == "check":
            S.cmd_check(eng, args.fix)
            if args.fix and args.apply:
                dst.cx.commit()
                log.info("maps commit ho gaye")
            else:
                dst.cx.rollback()
                if args.fix:
                    log.info("DRY-RUN tha: maps commit nahi hue. --apply --confirm-db %s se dobara chalao", config.DST["dbname"])
            eng.write_reports("check")
            return
        if args.cmd == "export-idmap":
            n = eng.idmap.export_json(args.out)
            log.info("%d entries -> %s", n, args.out)
            return
        if args.cmd == "recompute-script":
            return cmd_recompute_script(eng)
        steps = resolve_steps(args.steps)
        if args.cmd == "verify":
            S.cmd_verify(eng, steps)
            return
        if args.cmd == "rollback":
            S.cmd_rollback(eng, steps)
            if args.apply:
                dst.cx.commit()
            else:
                dst.cx.rollback()
                log.info("DRY-RUN: rollback commit nahi hua")
            return
        # ---- run
        warn_active_sessions(dst)
        if args.apply and not args.force_masters and any(n in S.CORE for n in steps):
            bad = S.preflight(eng)
            if bad:
                for b in bad:
                    log.error("MASTERS ADHOORE: %s", b)
                sys.exit("Zaroori masters map nahi hue. Pehle masters migrate karo (run_migration.sh) aur 'check --fix --apply', "
                         "phir dobara. (--force-masters se bypass, SIFARISH NAHI)")
        log.info("steps: %s | %s", ", ".join(steps), "APPLY (commit hoga)" if args.apply else
                 "DRY-RUN (aakhir mein ROLLBACK; id sequences phir bhi aage badh jati hain)")
        for name in steps:
            sp = S.SPECS[name]
            log.info("################ %s: %s", name, sp.desc)
            if sp.runner:
                sp.runner(eng)
            else:
                eng.run_spec(sp)
            n = eng.idmap.resolve_pending()
            if n:
                log.info("[%s] pending links lage: %d", name, n)
            if args.apply:
                dst.cx.commit()
        S.run_parent_paths(eng)
        left = dst.scalar("select count(*) from akg_pending")
        if left:
            log.warning("pending links jin ka target map nahi hua: %d (akg_pending table)", left)
        if args.apply:
            dst.cx.commit()
            log.info("COMMIT ho gaya. Ab: verify, phir recompute-script (Odoo shell)")
        else:
            dst.cx.rollback()
            log.info("DRY-RUN complete: ROLLBACK kar diya, destination mein kuch nahi badla. Report: tx_db_report.csv")
        eng.write_reports("tx_db")
    except KeyboardInterrupt:
        log.error("Ctrl+C: jo batches commit ho chuke wo mehfooz hain; dobara chalane par wahin se aage")
        try:
            dst.cx.rollback()
        except Exception:   # noqa
            pass
        eng.write_reports("tx_db")
        sys.exit(130)
    finally:
        log.info("waqt: %.0fs", time.time() - t0)


def cmd_gaps(eng, steps):
    """Read-only: har step ke liye columns ka match, data-loss (dropped) aur missing-required columns."""
    for name in steps:
        sp = S.SPECS[name]
        if sp.runner:
            continue
        st, dt = sp.model.replace(".", "_"), sp.dst_model.replace(".", "_")
        print("\n=== %s  (%s -> %s)" % (name, st, dt))
        if not eng.src.has_table(st) or not eng.dst.has_table(dt):
            print("   table nahi: source=%s dest=%s" % (eng.src.has_table(st), eng.dst.has_table(dt)))
            continue
        cnt = eng.src.scalar('select count(*) from "%s"' % st)
        ph = eng.build_plan(sp.model, sp.dst_model, sp.rename, sp.skip, src_table=st, dst_table=dt)
        print("   source rows: %d | columns copy honge: %d" % (cnt, len(ph.cols)))
        if ph.dropped:
            print("   NAHI JAYENGE (dst mein nahi/compatible nahi): " + ", ".join(sorted(ph.dropped)))
        if ph.missing_required:
            print("   DST NOT NULL, source se value nahi: " + ", ".join(sorted(ph.missing_required)))
        for ch in sp.children:
            cst, cdt = ch.model.replace(".", "_"), ch.dst_model.replace(".", "_")
            if not (eng.src.has_table(cst) and eng.dst.has_table(cdt)):
                print("   [%s] table nahi" % ch.model)
                continue
            p = eng.build_plan(ch.model, ch.dst_model, ch.rename, set(ch.skip) | {ch.fk},
                               drop_dst_cols={ch.rename.get(ch.fk) or ch.fk}, src_table=cst, dst_table=cdt)
            print("   -- %s: source rows %d | columns %d" % (ch.model, eng.src.scalar('select count(*) from "%s"' % cst), len(p.cols)))
            if p.dropped:
                print("      NAHI JAYENGE: " + ", ".join(sorted(p.dropped)))
            if p.missing_required:
                print("      DST NOT NULL, source se value nahi: " + ", ".join(sorted(p.missing_required)))
        m2m = eng.m2m_fields(sp.model, sp.dst_model, sp.rename, sp.m2m_skip)
        if m2m:
            print("   many2many copy: " + ", ".join(n for n, _, _ in m2m))


def cmd_recompute_script(eng):
    """Odoo 20 ke wo STORED COMPUTED fields jo source mein the hi nahi (naye) - unhe Odoo ORM se compute karwane ka shell script."""
    targets = {}
    for name in S.GROUPS["all"]:
        sp = S.SPECS[name]
        models = [(sp.model, sp.dst_model)] if not sp.runner else []
        models += [(c.model, c.dst_model) for c in sp.children]
        for sm, dm in models:
            sc = eng.src.columns(sm.replace(".", "_"))
            rows = eng.dst.q("select name from ir_model_fields where model = %s and store and compute is not null", (dm,))
            new = sorted(r["name"] for r in rows if r["name"] not in sc and r["name"] in eng.dst.columns(dm.replace(".", "_")))
            if new:
                targets[canon(sm)] = (dm, new)
    lines = ["# -*- coding: utf-8 -*-",
             "# Chalao:  odoo-bin shell -d <DST_DB> --no-http < post_recompute_shell.py",
             "# Sirf wo stored-computed fields jo Odoo 16 mein the hi nahi. Migrated ids = akg_idmap table (created = true).",
             "TARGETS = %r" % targets,
             "CHUNK = 500",
             "for src_model, (model, fields) in TARGETS.items():",
             "    env.cr.execute(\"select dst_id from akg_idmap where model = %s and created order by dst_id\", (src_model,))",
             "    ids = [r[0] for r in env.cr.fetchall()]",
             "    Model = env[model].with_context(active_test=False)",
             "    print(model, len(ids), 'records', fields)",
             "    for i in range(0, len(ids), CHUNK):",
             "        recs = Model.browse(ids[i:i + CHUNK]).exists()",
             "        for f in fields:",
             "            if f in Model._fields:",
             "                env.add_to_compute(Model._fields[f], recs)",
             "        recs.flush_recordset()",
             "        env.cr.commit()",
             "        env.invalidate_all()",
             "print('recompute done')"]
    open("post_recompute_shell.py", "w", encoding="utf-8").write("\n".join(lines) + "\n")
    for k, (dm, f) in targets.items():
        print("%-30s -> %s" % (dm, ", ".join(f)))
    print("\npost_recompute_shell.py ban gayi. Pehle STAGING par chalao.")


if __name__ == "__main__":
    main()
