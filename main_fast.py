import os
import sys
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
    print("❌ SUPABASE_URL ou SUPABASE_KEY manquants.", flush=True)
    sys.exit(1)

if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    print("❌ Paramètres Odoo manquants.", flush=True)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Connexion Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})

if not uid:
    print("❌ Impossible de s'authentifier sur Odoo.", flush=True)
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

    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.category", "search",
        [[("name", "=", "Forfaits eSIM")]], {"limit": 1})
    
    if ids:
        ESIM_CATEGORY_ID = ids[0]
        return ESIM_CATEGORY_ID

    ESIM_CATEGORY_ID = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.category", "create",
        [{"name": "Forfaits eSIM"}])
    return ESIM_CATEGORY_ID

def ensure_partner(email, first_name=None, last_name=None, supabase_id=None):
    """
    Crée ou trouve un partenaire en utilisant uniquement first_name et last_name.
    L'ID Supabase est stocké dans le champ 'ref' d'Odoo.
    """
    if not email:
        email = "client@fenuasim.com"
    
    email = email.strip().lower()

    # Recherche par email (insensible à la casse)
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
        [[("email", "=ilike", email)]], {"limit": 1})
    
    if ids:
        return ids[0]

    # Construction du nom (uniquement first_name et last_name)
    fullname = f"{first_name or ''} {last_name or ''}".strip() or email

    pid = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create", [{
        "name": fullname,
        "email": email,
        "ref": supabase_id, # Référence interne vers Supabase
        "customer_rank": 1
    }])

    print(f"🆕 Nouveau client Odoo : {fullname} ({email})", flush=True)
    return pid

def get_or_create_product(row):
    """
    Récupère ou crée le produit (sans mention de validité).
    """
    package_id = row.get("package_id")
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
        [[("default_code", "=", package_id)]], {"limit": 1})
    
    if ids:
        return ids[0]

    # Construction du nom (package_name + data) sans validité
    label_parts = []
    if row.get("package_name"):
        label_parts.append(row["package_name"])
    if row.get("data_amount") and row.get("data_unit"):
        label_parts.append(f"{row['data_amount']} {row['data_unit']}")

    name = " - ".join(label_parts) or "Forfait eSIM"
    
    # Logique de prix corrigée (XPF utilise 'amount' brut)
    currency = row.get("currency", "EUR").upper()
    if currency == "XPF":
        price = float(row.get("amount", 0))
    else:
        price = float(row.get("price") or (float(row.get("amount", 0)) / 100))

    pid = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "create", [{
        "name": name,
        "default_code": package_id,
        "type": "service",
        "list_price": price,
        "categ_id": get_or_create_esim_category(),
    }])

    print(f"🆕 Produit créé : {name} ({price} {currency})", flush=True)
    return pid

def find_order(stripe_session_id):
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "search",
        [[("client_order_ref", "=", stripe_session_id)]], {"limit": 1})
    return ids[0] if ids else None

def ensure_order_line(order_id, product_id, price, label):
    order = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "read",
        [[order_id], ["order_line"]])[0]

    if not order["order_line"]:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "write",
            [[order_id], {
                "order_line": [(0, 0, {
                    "product_id": product_id,
                    "name": label,
                    "product_uom_qty": 1,
                    "price_unit": price
                })]
            }])

def confirm_order(order_id):
    order = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "read",
        [[order_id], ["state"]])[0]

    if order["state"] not in ("sale", "done"):
        try:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "action_confirm", [[order_id]])
            print(f"✅ Commande confirmée : {order_id}", flush=True)
        except Exception as e:
            print(f"❌ Erreur confirmation {order_id} : {e}", flush=True)

# ============================================================
#  SYNCHRONISATION
# ============================================================

def sync_airalo():
    print("🔄 Sync Airalo…", flush=True)
    rows = supabase.table("airalo_orders").select("*").order("created_at").execute().data or []

    for row in rows:
        ref = f"AIRALO-{row['order_id']}"
        if find_order(ref): continue

        pid = ensure_partner(row.get("email"), row.get("prenom"), row.get("nom"))

        note = f"ICCID: {row.get('sim_iccid')}<br/>QR: {row.get('qr_code_url')}"
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "create", [{
            "partner_id": pid,
            "client_order_ref": ref,
            "origin": "Airalo",
            "note": note
        }])
    print("✅ Airalo synchronisé.", flush=True)

def sync_stripe():
    print("💳 Sync Stripe…", flush=True)
    rows = supabase.table("orders").select("*").eq("status", "completed").order("created_at").execute().data or []

    for row in rows:
        ref = row["stripe_session_id"]
        currency = row.get("currency", "EUR").upper()
        promo = row.get("promo_code")
        
        # Calcul du prix corrigé
        if currency == "XPF":
            price = float(row.get("amount", 0))
        else:
            price = float(row.get("price") or (float(row.get("amount", 0)) / 100))

        order_id = find_order(ref)
        pid = ensure_partner(row.get("email"), row.get("first_name"), row.get("last_name"), row.get("id"))
        
        # Mise à jour Supabase avec le partner_id Odoo
        supabase.table("orders").update({"partner_id": pid}).eq("id", row["id"]).execute()

        product_id = get_or_create_product(row)
        label = row.get("package_name") or "Forfait eSIM"

        if not order_id:
            note_html = f"""
            <p><strong>Commande eSIM FENUA SIM</strong></p>
            <p>
            <strong>Destination :</strong> {row.get('destination_name', 'N/A')}<br/>
            <strong>Forfait :</strong> {label}<br/>
            <strong>Données :</strong> {row.get('data_amount')} {row.get('data_unit')}<br/>
            <strong>Email client :</strong> {row.get('email')}
            </p>
            """
            if promo:
                note_html += f"<p><strong>Code Promo :</strong> {promo}</p>"

            order_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "create", [{
                "partner_id": pid,
                "client_order_ref": ref,
                "origin": "Stripe",
                "note": note_html,
            }])
            print(f"🧾 Nouvelle commande : {ref}", flush=True)

        ensure_order_line(order_id, product_id, price, label)
        confirm_order(order_id)
    print("✅ Stripe synchronisé.", flush=True)

def sync_emails():
    print("📨 Sync emails_sent…", flush=True)
    rows = supabase.table("emails_sent").select("*").eq("archived_odoo", False).execute().data or []
    for row in rows:
        partner_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search",
            [[("email", "=", row.get("email"))]], {"limit": 1})
        
        if partner_ids:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "mail.message", "create", [{
                "model": "res.partner",
                "res_id": partner_ids[0],
                "subject": row.get("subject"),
                "body": row.get("html"),
                "message_type": "comment",
                "subtype_id": 1,
            }])
            supabase.table("emails_sent").update({"archived_odoo": True}).eq("id", row["id"]).execute()
    print("✅ Emails archivés.", flush=True)

if __name__ == "__main__":
    print("🚀 SCRIPT DEMARRÉ", flush=True)
    sync_airalo()
    sync_stripe()
    sync_emails()
    print("✅ SCRIPT TERMINÉ", flush=True)
