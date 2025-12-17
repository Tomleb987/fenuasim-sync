import os
import sys
import time
import xmlrpc.client
from supabase import create_client, Client

# ============================================================
#  CONFIG SUPABASE & ODOO
# ============================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE_URL ou SUPABASE_KEY manquants.")
    sys.exit(1)

if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    print("❌ Paramètres Odoo manquants.")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Connexion Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    print("❌ Impossible de s'authentifier sur Odoo.")
    sys.exit(1)

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

ESIM_CATEGORY_ID = None


# ============================================================
#  HELPERS
# ============================================================

def get_or_create_esim_category():
    global ESIM_CATEGORY_ID
    if ESIM_CATEGORY_ID:
        return ESIM_CATEGORY_ID

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.category", "search",
        [[("name", "=", "Forfaits eSIM")]],
        {"limit": 1}
    )
    if ids:
        ESIM_CATEGORY_ID = ids[0]
        return ESIM_CATEGORY_ID

    ESIM_CATEGORY_ID = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.category", "create",
        [{"name": "Forfaits eSIM"}]
    )

    print("🆕 Catégorie : Forfaits eSIM créée.")
    return ESIM_CATEGORY_ID


def ensure_partner(email, first_name=None, last_name=None):
    if not email:
        email = "client@fenuasim.com"

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[("email", "=", email)]],
        {"limit": 1}
    )
    if ids:
        return ids[0]

    fullname = f"{first_name or ''} {last_name or ''}".strip()
    if not fullname:
        fullname = email

    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{"name": fullname, "email": email}]
    )

    print(f"🆕 Nouveau client Odoo : {email}")
    return pid


def get_or_create_product(row):
    package_id = row.get("package_id") or f"ESIM-{row.get('data_amount')}"

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search",
        [[("default_code", "=", package_id)]],
        {"limit": 1}
    )
    if ids:
        return ids[0]

    label_parts = []
    if row.get("package_name"):
        label_parts.append(row["package_name"])
    if row.get("data_amount") and row.get("data_unit"):
        label_parts.append(f"{row['data_amount']} {row['data_unit']}")
    if row.get("validity"):
        label_parts.append(f"{row['validity']} jours")

    name = " - ".join(label_parts) or "Forfait eSIM"
    price = row.get("price") or float(row.get("amount", 0)) / 100

    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "create",
        [{
            "name": name,
            "default_code": package_id,
            "type": "service",
            "list_price": price,
            "categ_id": get_or_create_esim_category(),
            "taxes_id": [(6, 0, [])],
        }]
    )

    print(f"🆕 Produit créé : {name}")
    return pid


def find_order(stripe_session_id):
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search",
        [[("client_order_ref", "=", stripe_session_id)]],
        {"limit": 1}
    )
    return ids[0] if ids else None


def ensure_order_line(order_id, product_id, price, label):
    order = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "read",
        [[order_id], ["order_line"]]
    )[0]

    if order["order_line"]:
        return

    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "write",
        [[order_id], {
            "order_line": [(0, 0, {
                "product_id": product_id,
                "name": label,
                "product_uom_qty": 1,
                "price_unit": price
            })]
        }]
    )


def confirm_order(order_id):
    order = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "read",
        [[order_id], ["state"]]
    )[0]

    if order["state"] in ("sale", "done"):
        print(f"ℹ️ Commande déjà confirmée : {order_id}")
        return

    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_confirm",
            [[order_id]]
        )
        print(f"✅ Commande confirmée : {order_id}")
    except Exception as e:
        print("❌ Erreur confirmation :", e)


# ============================================================
#  SYNC STRIPE
# ============================================================

def sync_stripe():
    print("💳 Sync Stripe…")

    rows = (
        supabase.table("orders")
        .select("*")
        .order("created_at")
        .execute()
        .data
        or []
    )

    print(f"📄 {len(rows)} lignes Stripe.")

    for row in rows:
        if row.get("status") != "completed":
            continue

        ref = row["stripe_session_id"]
        print(f"🔎 Stripe session {ref}")

        order_id = find_order(ref)
        pid = ensure_partner(row.get("email"), row.get("first_name"), row.get("last_name"))
        supabase.table("orders").update({"partner_id": pid}).eq("id", row["id"]).execute()

        product_id = get_or_create_product(row)

        price = row.get("price") or float(row.get("amount", 0)) / 100
        label = row.get("package_name") or "Forfait eSIM"

        note_html = f"""
        <p><strong>Commande eSIM FENUA SIM</strong></p>
        <p>
        <strong>Destination :</strong> {row.get('destination_name', 'N/A')}<br/>
        <strong>Forfait :</strong> {row.get('package_name', 'eSIM')}<br/>
        <strong>Données :</strong> {row.get('data_amount')} {row.get('data_unit')}<br/>
        <strong>Validité :</strong> {row.get('validity')} jours<br/>
        <strong>Email client :</strong> {row.get('email')}
        </p>
        """

        if row.get("qr_code_url"):
            note_html += f"""
            <p><strong>QR Code :</strong><br/>
            <img src="{row['qr_code_url']}" width="180"/></p>
            """

        if not order_id:
            order_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "sale.order", "create",
                [{
                    "partner_id": pid,
                    "client_order_ref": ref,
                    "origin": "Stripe",
                    "note": note_html,
                }]
            )
            print(f"🧼 Nouvelle commande Odoo : {order_id}")

        ensure_order_line(order_id, product_id, price, label)
        confirm_order(order_id)

    print("✅ Stripe synchronisé.")


# Le reste du fichier (sync_airalo, sync_emails, main) ne change pas et peut être laissé tel quel.
