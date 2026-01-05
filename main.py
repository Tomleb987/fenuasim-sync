import os
import xmlrpc.client
from datetime import datetime
from supabase import create_client

# -----------------------------------------
# CONFIG
# -----------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)


# -----------------------------------------
# UTILS
# -----------------------------------------
def normalize_date(val):
    if not val:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_partner(row):
    """
    AMÉLIORATION : Utilise uniquement first_name et last_name.
    Stocke l'ID Supabase dans le champ 'ref' d'Odoo.
    """
    email = row.get("email", "").strip().lower()
    if not email:
        email = "client@fenuasim.com"

    # Recherche par email (insensible à la casse)
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
        [[["email", "=ilike", email]]],
        {"limit": 1}
    )
    if res:
        return res[0]

    # Construction du nom (uniquement first_name et last_name)
    fname = row.get("first_name") or ""
    lname = row.get("last_name") or ""
    fullname = f"{fname} {lname}".strip() or email

    pid = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "create",
        [{
            "name": fullname,
            "email": email,
            "ref": row.get("id"), # ID unique de Supabase
            "customer_rank": 1
        }]
    )
    print(f"👤 Partner créé : {fullname} ({email})")
    return pid


def find_product(package_id):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1}
    )
    return res[0] if res else None


def find_odoo_order(ref):
    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search",
        [[["client_order_ref", "=", ref]]],
        {"limit": 1}
    )
    return res[0] if res else None


def confirm_order(order_id):
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_confirm",
            [[order_id]]
        )
        print(f"🟢 Commande confirmée : {order_id}")
    except Exception as e:
        print(f"❌ Erreur confirmation {order_id} : {e}")


# -----------------------------------------
# SYNC PRODUITS (Retrait de la validité)
# -----------------------------------------
def sync_products():
    print("📦 Sync produits Airalo...")
    data = supabase.table("airalo_packages").select("*").execute().data

    for row in data:
        pkg = row["id"]
        name = row["name"]
        region = row["region"]
        price = row["price"]

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[["default_code", "=", pkg]]],
            {"fields": ["id"], "limit": 1}
        )

        vals = {
            "name": f"{name} [{region}]" if region else name,
            "default_code": pkg,
            "list_price": float(price),
            "type": "service",
        }

        if existing:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "write",
                [[existing[0]["id"]], vals]
            )
            print(f"🔁 Produit mis à jour : {pkg}")
        else:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "create",
                [vals]
            )
            print(f"✨ Produit créé : {pkg}")

    print("✅ Produits synchronisés.")


# -----------------------------------------
# SYNC AIRALO ORDERS
# -----------------------------------------
def sync_airalo_orders():
    print("📡 Sync Airalo orders…")
    rows = supabase.table("airalo_orders").select("*").execute().data

    for row in rows:
        order_ref = row.get("order_id")
        email = row.get("email")
        package_id = row.get("package_id")
        created_at = normalize_date(row.get("created_at"))

        if not order_ref or not package_id or not email:
            continue

        if find_odoo_order(order_ref):
            continue

        product = find_product(package_id)
        if not product: continue

        # On adapte la ligne pour ensure_partner
        partner_id = ensure_partner({
            "email": email,
            "first_name": row.get("prenom"),
            "last_name": row.get("nom")
        })

        order_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "date_order": created_at,
                "origin": "Airalo",
                "order_line": [
                    (0, 0, {
                        "product_id": product["id"],
                        "name": product["name"],
                        "product_uom_qty": 1,
                        "price_unit": product["list_price"],
                    })
                ],
            }]
        )
        print(f"🟢 Commande Airalo créée : {order_ref}")


# -----------------------------------------
# SYNC STRIPE PAYMENTS (Amélioré avec XPF et Promo)
# -----------------------------------------
def sync_stripe_payments():
    print("💳 Sync Stripe payments…")
    rows = supabase.table("orders").select("*").eq("status", "completed").execute().data

    for row in rows:
        order_ref = row["stripe_session_id"] # Utilise stripe_session_id pour la cohérence
        currency = row.get("currency", "EUR").upper()
        promo = row.get("promo_code")

        # CORRECTION PRIX XPF 
        if currency == "XPF":
            price = float(row.get("amount", 0))
        else:
            price = float(row.get("price") or (float(row.get("amount", 0)) / 100))

        odoo_order_id = find_odoo_order(order_ref)
        partner_id = ensure_partner(row)

        # Si la commande n'existe pas, on la crée (plus robuste)
        if not odoo_order_id:
            product = find_product(row.get("package_id"))
            if not product: continue

            # Note SANS validité 
            note_html = f"""
            <p><strong>Commande eSIM FENUA SIM</strong></p>
            <p>
            <strong>Destination :</strong> {row.get('destination_name', 'N/A')}<br/>
            <strong>Forfait :</strong> {row.get('package_name', 'eSIM')}<br/>
            <strong>Données :</strong> {row.get('data_amount')} {row.get('data_unit')}<br/>
            <strong>Email client :</strong> {row.get('email')}
            </p>
            """
            if promo:
                note_html += f"<p><strong>Code Promo :</strong> {promo}</p>"

            odoo_order_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "sale.order", "create",
                [{
                    "partner_id": partner_id,
                    "client_order_ref": order_ref,
                    "origin": "Stripe",
                    "note": note_html,
                    "order_line": [
                        (0, 0, {
                            "product_id": product["id"],
                            "name": product["name"],
                            "product_uom_qty": 1,
                            "price_unit": price,
                        })
                    ],
                }]
            )

        confirm_order(odoo_order_id)


# -----------------------------------------
# MAIN
# -----------------------------------------
if __name__ == "__main__":
    print("🚀 FULL SYNC STARTED")
    sync_products()
    sync_airalo_orders()
    sync_stripe_payments()
    print("🎉 FULL SYNC DONE")
