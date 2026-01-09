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

if not all([SUPABASE_URL, SUPABASE_KEY, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    raise SystemExit("❌ Variables d'environnement manquantes (Supabase/Odoo).")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise SystemExit("❌ Auth Odoo impossible.")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

# -----------------------------------------
# CONSTANTES
# -----------------------------------------
XPF_PER_EUR = 119.33  # parité fixe

# -----------------------------------------
# UTILS
# -----------------------------------------
def normalize_date(val):
    if not val:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def compute_price_eur_from_order_row(row) -> float:
    """
    Objectif: Odoo doit recevoir TOUJOURS un prix en EUR.
    - Si currency == EUR : amount est en centimes -> /100
    - Si currency == XPF : amount est en XPF -> /119.33
    """
    currency = (row.get("currency") or "EUR").upper()
    amount = float(row.get("amount") or 0)

    if amount <= 0:
        raise ValueError("amount manquant ou <= 0")

    if currency == "EUR":
        return round(amount / 100.0, 2)

    if currency == "XPF":
        return round(amount / XPF_PER_EUR, 2)

    # Si tu ajoutes d'autres devises plus tard, traite ici (USD etc.)
    raise ValueError(f"Devise non gérée: {currency}")


def ensure_partner(row):
    """
    Utilise uniquement first_name et last_name.
    Stocke l'ID Supabase dans 'ref' si présent.
    """
    email = (row.get("email") or "").strip().lower() or "client@fenuasim.com"

    res = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[["email", "=ilike", email]]],
        {"limit": 1}
    )
    if res:
        return res[0]

    fname = row.get("first_name") or ""
    lname = row.get("last_name") or ""
    fullname = f"{fname} {lname}".strip() or email

    vals = {
        "name": fullname,
        "email": email,
        "customer_rank": 1
    }
    if row.get("id"):
        vals["ref"] = str(row.get("id"))

    pid = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create", [vals])
    print(f"👤 Partner créé : {fullname} ({email})", flush=True)
    return pid


def find_product(package_id):
    if not package_id:
        return None
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


def read_order_total(order_id) -> float:
    rec = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "read",
        [[order_id], ["amount_total"]]
    )[0]
    return float(rec.get("amount_total") or 0.0)


def confirm_order(order_id, expected_total=None):
    """
    Confirme seulement si le total est cohérent (optionnel mais très utile).
    """
    try:
        if expected_total is not None:
            total = read_order_total(order_id)
            # tolérance d'arrondi
            if abs(total - float(expected_total)) > 0.05:
                print(f"⚠️ Pas de confirmation: total Odoo={total:.2f} EUR vs attendu={expected_total:.2f} EUR (order {order_id})", flush=True)
                return

        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "action_confirm", [[order_id]])
        print(f"🟢 Commande confirmée : {order_id}", flush=True)
    except Exception as e:
        print(f"❌ Erreur confirmation {order_id} : {e}", flush=True)


# -----------------------------------------
# SYNC PRODUITS
# -----------------------------------------
def sync_products():
    print("📦 Sync produits Airalo...", flush=True)
    data = supabase.table("airalo_packages").select("*").execute().data or []

    for row in data:
        pkg = row.get("id")
        if not pkg:
            continue

        name = row.get("name") or pkg
        region = row.get("region")
        price = float(row.get("price") or 0)

        existing = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[["default_code", "=", pkg]]],
            {"fields": ["id"], "limit": 1}
        )

        vals = {
            "name": f"{name} [{region}]" if region else name,
            "default_code": pkg,
            "list_price": price,    # ici tu es déjà en EUR (table airalo_packages)
            "type": "service",
        }

        if existing:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "write", [[existing[0]["id"]], vals])
            print(f"🔁 Produit mis à jour : {pkg}", flush=True)
        else:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "create", [vals])
            print(f"✨ Produit créé : {pkg}", flush=True)

    print("✅ Produits synchronisés.", flush=True)


# -----------------------------------------
# SYNC AIRALO ORDERS
# -----------------------------------------
def sync_airalo_orders():
    print("📡 Sync Airalo orders…", flush=True)
    rows = supabase.table("airalo_orders").select("*").execute().data or []

    for row in rows:
        order_ref = row.get("order_id")
        email = row.get("email")
        package_id = row.get("package_id")
        created_at = normalize_date(row.get("created_at"))

        if not order_ref or not package_id or not email:
            continue

        # Optionnel: prefix pour éviter collisions avec Stripe
        odoo_ref = f"AIRALO-{order_ref}"

        if find_odoo_order(odoo_ref):
            continue

        product = find_product(package_id)
        if not product:
            continue

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
                "client_order_ref": odoo_ref,
                "date_order": created_at,
                "origin": "Airalo",
                "order_line": [
                    (0, 0, {
                        "product_id": product["id"],
                        "name": product["name"],
                        "product_uom_qty": 1,
                        "price_unit": float(product["list_price"]),  # EUR
                    })
                ],
            }]
        )
        print(f"🟢 Commande Airalo créée : {odoo_ref} (id {order_id})", flush=True)


# -----------------------------------------
# SYNC STRIPE PAYMENTS (EUR only dans Odoo)
# -----------------------------------------
def sync_stripe_payments():
    print("💳 Sync Stripe payments…", flush=True)
    rows = supabase.table("orders").select("*").eq("status", "completed").execute().data or []

    for row in rows:
        order_ref = row.get("stripe_session_id")
        if not order_ref:
            continue

        # Anti-doublon
        odoo_order_id = find_odoo_order(order_ref)
        if odoo_order_id:
            continue

        # ✅ Prix EUR calculé proprement (clé du fix)
        try:
            price_eur = compute_price_eur_from_order_row(row)
        except Exception as e:
            print(f"❌ Skip {order_ref} : {e}", flush=True)
            continue

        currency_paid = (row.get("currency") or "EUR").upper()
        amount_paid = row.get("amount")  # montant encaissement d'origine
        promo = row.get("promo_code")

        partner_id = ensure_partner(row)

        product = find_product(row.get("package_id"))
        if not product:
            continue

        note_html = f"""
        <p><strong>Commande eSIM FENUA SIM</strong></p>
        <p>
        <strong>Destination :</strong> {row.get('destination_name', 'N/A')}<br/>
        <strong>Forfait :</strong> {row.get('package_name', 'eSIM')}<br/>
        <strong>Données :</strong> {row.get('data_amount')} {row.get('data_unit')}<br/>
        <strong>Email client :</strong> {row.get('email')}<br/>
        <strong>Paiement Stripe :</strong> {amount_paid} {currency_paid}<br/>
        <strong>Montant enregistré Odoo :</strong> {price_eur:.2f} EUR
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
                        "price_unit": float(price_eur),  # ✅ EUR uniquement
                    })
                ],
            }]
        )
        print(f"🧾 Commande Stripe créée : {order_ref} -> {price_eur:.2f} EUR (id {odoo_order_id})", flush=True)

        # ✅ Confirme seulement si le total correspond
        confirm_order(odoo_order_id, expected_total=price_eur)


# -----------------------------------------
# MAIN
# -----------------------------------------
if __name__ == "__main__":
    print("🚀 FULL SYNC STARTED", flush=True)
    sync_products()
    sync_airalo_orders()
    sync_stripe_payments()
    print("🎉 FULL SYNC DONE", flush=True)
