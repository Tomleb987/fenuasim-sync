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

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    print("❌ Impossible de s'authentifier sur Odoo.")
    sys.exit(1)

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

ESIM_CATEGORY_ID = None  # cache


# ============================================================
#  HELPERS
# ============================================================

def get_or_create_esim_category():
    """Catégorie 'Forfaits eSIM'."""
    global ESIM_CATEGORY_ID
    if ESIM_CATEGORY_ID:
        return ESIM_CATEGORY_ID

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'product.category', 'search',
        [[('name', '=', 'Forfaits eSIM')]],
        {'limit': 1}
    )
    if ids:
        ESIM_CATEGORY_ID = ids[0]
        return ESIM_CATEGORY_ID

    ESIM_CATEGORY_ID = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'product.category', 'create',
        [{'name': 'Forfaits eSIM'}]
    )
    print(f"🆕 Catégorie créée : Forfaits eSIM")
    return ESIM_CATEGORY_ID


def ensure_partner(email, first_name=None, last_name=None):
    if not email:
        email = "client@fenuasim.com"

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'res.partner', 'search',
        [[('email', '=', email)]],
        {'limit': 1}
    )
    if ids:
        return ids[0]

    name = (first_name or "") + " " + (last_name or "")
    name = name.strip() or email

    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'res.partner', 'create',
        [{'name': name, 'email': email}]
    )
    print(f"🆕 Nouveau contact : {email}")
    return pid


def get_or_create_product(row):
    """Produit basé sur package_id."""
    package_id = row.get("package_id")
    if not package_id:
        package_id = f"ESIM-{row.get('data_amount')}"

    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'product.product', 'search',
        [[('default_code', '=', package_id)]],
        {'limit': 1}
    )
    if ids:
        return ids[0]

    name_parts = []
    if row.get("package_name"):
        name_parts.append(row["package_name"])
    if row.get("data_amount") and row.get("data_unit"):
        name_parts.append(f"{row['data_amount']} {row['data_unit']}")
    if row.get("validity"):
        name_parts.append(f"{row['validity']} jours")

    label = " - ".join(name_parts) or "Forfait eSIM"

    price = row.get("price")
    if not price:
        price = float(row.get("amount", 0)) / 100

    categ_id = get_or_create_esim_category()

    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'product.product', 'create',
        [{
            'name': label,
            'default_code': package_id,
            'type': 'service',
            'detailed_type': 'service',
            'list_price': price,
            'categ_id': categ_id,
            'taxes_id': [(6, 0, [])],  # TVA = 0
        }]
    )
    print(f"🆕 Produit créé : {label}")
    return pid


def find_order(stripe_id):
    ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'search',
        [[('client_order_ref', '=', stripe_id)]],
        {'limit': 1}
    )
    return ids[0] if ids else None


def ensure_order_line(order_id, product_id, price, label):
    so = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'read',
        [[order_id], ['order_line']]
    )[0]

    if so['order_line']:
        return  # ok

    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'write',
        [[order_id], {
            'order_line': [(0, 0, {
                'product_id': product_id,
                'name': label,
                'product_uom_qty': 1,
                'price_unit': price
            })]
        }]
    )


def confirm_order(order_id):
    """Confirme la commande si pas déjà confirmée."""
    try:
        order = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'read',
            [[order_id], ['state']]
        )[0]
        if order['state'] in ('sale', 'done'):
            print(f"ℹ️ Commande déjà confirmée (ID {order_id})")
            return

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'action_confirm',
            [[order_id]]
        )
        print(f"✅ Commande confirmée (ID {order_id})")
    except Exception as e:
        print(f"❌ Erreur confirmation : {e}")


# ============================================================
#  SYNC AIRALO
# ============================================================

def sync_airalo():
    print("🔄 Sync Airalo…")

    res = supabase.table("airalo_orders").select("*").order("created_at").execute()
    rows = res.data or []

    print(f"📄 {len(rows)} lignes Airalo trouvées.")

    for row in rows:
        ref = f"AIRALO-{row['order_id']}"

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search',
            [[('client_order_ref', '=', ref)]],
            {'limit': 1}
        )
        if existing:
            continue

        pid = ensure_partner(row.get("email"), row.get("prenom"), row.get("nom"))

        note = f"""Commande Airalo
ICCID: {row.get('sim_iccid')}
QR: {row.get('qr_code_url')}
Apple: {row.get('apple_installation_url')}
Data: {row.get('data_balance')}
Statut: {row.get('status')}
Date: {row.get('created_at')}
"""

        so_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'create',
            [{
                'partner_id': pid,
                'client_order_ref': ref,
                'origin': 'Airalo',
                'note': note
            }]
        )
    print("✅ Airalo synchronisé.")


# ============================================================
#  SYNC STRIPE
# ============================================================

def sync_stripe():
    print("💳 Sync Stripe…")

    rows = supabase.table("orders").select("*").order("created_at").execute().data or []
    print(f"📄 {len(rows)} lignes Stripe.")

    for row in rows:
        if row.get("status") != "completed":
            continue

        ref = row["stripe_session_id"]
        print(f"🔎 Stripe session {ref}")

        order_id = find_order(ref)

        pid = ensure_partner(row.get("email"), row.get("first_name"), row.get("last_name"))
        prod = get_or_create_product(row)

        price = row.get("price")
        if not price:
            price = float(row.get("amount", 0)) / 100

        label = prod

        if not order_id:
            order_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'sale.order', 'create',
                [{
                    'partner_id': pid,
                    'client_order_ref': ref,
                    'origin': 'Stripe',
                }]
            )
            print(f"🧾 Nouvelle commande Odoo : {order_id}")

        ensure_order_line(order_id, prod, price, row.get("package_name"))
        confirm_order(order_id)

    print("✅ Stripe synchronisé.")


# ============================================================
#  MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 FAST SAAS STARTED")

    sync_airalo()
    sync_stripe()

    print("✅ FAST SAAS DONE")
