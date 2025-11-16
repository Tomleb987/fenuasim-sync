import os
import requests
from datetime import datetime
from supabase import create_client
import xmlrpc.client

# -----------------------------
# 🔗 Connexions Supabase et Odoo
# -----------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

print("🚀 FAST SYNC STARTED")


# -----------------------------
# 📌 Helpers
# -----------------------------
def normalize_date(date_str):
    if not date_str:
        return None
    try:
        if isinstance(date_str, datetime):
            return date_str.strftime("%Y-%m-%d %H:%M:%S")
        return datetime.fromisoformat(date_str.replace("Z", "")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except:
        return None


def find_partner_by_email(email):
    res = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "search_read",
        [[["email", "=", email]]],
        {"fields": ["id", "name"], "limit": 1},
    )
    return res[0] if res else None


def create_partner(email, name):
    pid = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "create",
        [{"name": name, "email": email}],
    )
    print(f"🆕 Partner créé : {email} (ID {pid})")
    return {"id": pid, "name": name}


def find_odoo_order(order_ref):
    res = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "search_read",
        [[["client_order_ref", "=", order_ref]]],
        {"fields": ["id", "state"], "limit": 1},
    )
    return res[0] if res else None


def create_odoo_order(partner_id, product_id, order_ref):
    sale_id = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "create",
        [
            {
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "order_line": [
                    [
                        0,
                        0,
                        {
                            "product_id": product_id,
                            "product_uom_qty": 1,
                            "price_unit": 0,
                        },
                    ]
                ],
            }
        ],
    )
    print(f"🟢 Commande Airalo créée : {order_ref}")
    return sale_id


def confirm_order(order_id):
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "action_confirm", [[order_id]]
        )
        print(f"✔️ Commande confirmée (ID {order_id})")
    except Exception as e:
        print("⚠️ Impossible de confirmer :", e)


def mark_as_paid(order_id):
    try:
        models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "write",
            [[order_id], {"state": "sale"}],
        )
        print(f"💰 Commande marquée PAYÉE (ID {order_id})")
    except Exception as e:
        print("⚠️ Impossible de passer en payé :", e)


# -----------------------------
# 🔄 Sync Airalo → Odoo
# -----------------------------
def sync_airalo_orders():
    print("🔄 Sync Airalo orders…")

    data = (
        supabase.table("airalo_orders")
        .select("*")
        .order("created_at", desc=False)
        .execute()
    ).data

    print(f"📄 {len(data)} lignes Airalo récupérées.")

    for row in data:
        email = row.get("email")
        order_ref = str(row.get("order_id"))
        package_id = row.get("package_id")
        created_at = normalize_date(row.get("created_at"))

        # Check/existing partner
        partner = find_partner_by_email(email)
        if not partner:
            partner = create_partner(email, email)

        # Already existing order ?
        odoo_order = find_odoo_order(order_ref)
        if odoo_order:
            continue

        # Create order
        sale_id = create_odoo_order(partner["id"], 1, order_ref)  # Product ignored (fast)
        confirm_order(sale_id)


# -----------------------------
# 💳 Sync Stripe Payments → Odoo
# -----------------------------
def sync_stripe_payments():
    print("💳 Sync Stripe…")

    rows = supabase.table("orders").select("*").order("created_at").execute().data
    print(f"📄 {len(rows)} lignes orders récupérées.")

    for row in rows:
        email = row.get("email")
        order_ref = row.get("order_id")
        status = (row.get("status") or "").lower().strip()

        print(f"🔎 Stripe row → {email} | status={status}")

        # ---------------------------------------------
        # ❗ Fix principal : ignorer les lignes sans order_id
        # ---------------------------------------------
        if not order_ref:
            print(f"⚠️ Ignoré : order_id manquant pour {email}")
            continue

        # Status must be completed
        if status != "completed":
            continue

        # Check order
        odoo_order = find_odoo_order(order_ref)
        if not odoo_order:
            print(f"⚠️ Commande Stripe mais pas trouvée dans Odoo → {order_ref}")
            continue

        # Mark as paid
        mark_as_paid(odoo_order["id"])


# -----------------------------
# 🚀 MAIN
# -----------------------------
sync_airalo_orders()
sync_stripe_payments()

print("✅ FAST SYNC DONE")
