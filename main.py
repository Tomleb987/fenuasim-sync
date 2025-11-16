import os
import base64
import requests
import xmlrpc.client
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# 🔐 Config
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not all([SUPABASE_URL, SUPABASE_KEY, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
    raise RuntimeError("❌ Variables d'environnement manquantes. Vérifie SUPABASE_* et ODOO_*")

# 🔗 Connexions
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise Exception("❌ Connexion Odoo échouée")

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# 🕒 Normalisation des dates pour Odoo (format YYYY-MM-DD HH:MM:SS)
def normalize_odoo_datetime(value: str | None) -> str:
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # On gère les formats ISO avec ou sans timezone
    try:
        # Ex : "2025-11-13 22:45:53.511+00"
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        # On enlève l'info de timezone pour coller à ce que veut Odoo SaaS
        return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 📸 URL d'image → base64
def get_image_base64_from_url(url: str | None):
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return base64.b64encode(resp.content).decode("utf-8")
        else:
            print(f"⚠️ Erreur téléchargement image ({resp.status_code}) : {url}")
    except Exception as e:
        print(f"❌ Exception image {url}: {e}")
    return None


# 🧹 Suppression de doublons produits (même default_code)
def remove_duplicate_products():
    print("🧹 Suppression des doublons produits...")

    products = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [[["default_code", "!=", False]]],
        {"fields": ["id", "default_code"], "limit": 5000},
    )

    from collections import defaultdict
    grouped = defaultdict(list)
    for p in products:
        grouped[p["default_code"]].append(p["id"])

    total_deleted = 0
    for code, ids in grouped.items():
        if len(ids) > 1:
            to_delete = ids[1:]
            try:
                models.execute_kw(
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "product.product",
                    "unlink",
                    [to_delete],
                )
                total_deleted += len(to_delete)
                print(f"🗑️ Doublons supprimés pour {code}: {len(to_delete)}")
            except Exception as e:
                print(f"❌ Erreur suppression doublons {code}: {e}")

    print(f"✅ Nettoyage terminé ({total_deleted} doublons supprimés)")


# 🔄 Création / MAJ d’un produit depuis airalo_packages
def upsert_product(row: dict):
    package_id = row.get("airalo_id")
    name_base = row.get("name")
    region = row.get("region") or ""
    price = row.get("final_price_eur") or row.get("price_eur") or 0.0
    description = row.get("description") or ""
    data_amount = row.get("data_amount")
    data_unit = row.get("data_unit")
    validity_days = row.get("validity_days")
    image_url = row.get("image_url")

    if not package_id or not name_base:
        return

    name = f"{name_base} [{region}]" if region else name_base

    # Description (optionnel)
    desc_lines = []
    if description:
        desc_lines.append(description)
    if data_amount and data_unit and validity_days:
        desc_lines.append(f"{data_amount} {data_unit} pour {validity_days} jours")
    if region:
        desc_lines.append(f"Région : {region}")
    full_description = "\n".join(desc_lines)

    # Produit existant ?
    existing = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "product_tmpl_id", "image_1920"], "limit": 1},
    )

    image_b64 = None
    if image_url:
        image_b64 = get_image_base64_from_url(image_url)

    if existing:
        prod = existing[0]
        tmpl_id = prod["product_tmpl_id"][0]

        vals = {
            "name": name,
            "list_price": float(price),
            "description": full_description,
        }
        if image_b64 and not prod["image_1920"]:
            vals["image_1920"] = image_b64

        models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.template",
            "write",
            [[tmpl_id], vals],
        )
        print(f"🔁 Produit mis à jour : {package_id}")
        return

    # Création
    vals = {
        "name": name,
        "default_code": package_id,
        "list_price": float(price),
        "type": "service",
        "sale_ok": True,
        "purchase_ok": False,
        "description": full_description,
    }
    if image_b64:
        vals["image_1920"] = image_b64

    product_id = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "create",
        [vals],
    )
    print(f"✅ Produit créé : {package_id} — ID {product_id}")


def sync_airalo_packages():
    print("🚀 Sync produits Airalo...")
    packages = supabase.table("airalo_packages").select("*").execute().data
    print(f"📦 {len(packages)} packages trouvés")
    for row in packages:
        upsert_product(row)
    print("🎉 Produits Airalo synchronisés.")


# 👤 Trouver / créer un partenaire basé sur l’email (clé unique)
def find_or_create_partner(email: str | None, full_name: str | None = None):
    if not email:
        return None

    email_clean = email.strip().lower()

    # Recherche par email
    partners = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "search",
        [[["email", "=", email_clean]]],
        {"limit": 1},
    )
    if partners:
        return partners[0]

    # Création
    partner_id = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "create",
        [{
            "name": full_name or email_clean,
            "email": email_clean,
            "customer_rank": 1,
        }],
    )
    print(f"👤 Nouveau client créé : {email_clean}")
    return partner_id


# 🔎 Trouver un produit Odoo par default_code = package_id
def find_product(package_id: str | None):
    if not package_id:
        return None
    product = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [[["default_code", "=", package_id]]],
        {"fields": ["id", "name", "list_price"], "limit": 1},
    )
    return product[0] if product else None


# 📚 Index Airalo : (email, package_id) → liste de lignes airalo_orders
def build_airalo_index():
    print("📚 Chargement airalo_orders pour enrichissement eSIM...")
    rows = supabase.table("airalo_orders").select("*").execute().data
    index: dict[tuple[str, str], list[dict]] = {}

    for r in rows:
        email = (r.get("email") or "").strip().lower()
        package_id = r.get("package_id") or ""
        if not email or not package_id:
            continue
        key = (email, package_id)
        index.setdefault(key, []).append(r)

    # On ne trie pas forcément, on choisira plus tard la plus proche en date
    print(f"📚 Index Airalo construit ({len(rows)} lignes)")
    return index


# 🧩 Trouver la ligne Airalo la plus proche en temps pour une commande donnée
def match_airalo_row(order_row: dict, airalo_index: dict):
    email = (order_row.get("email") or "").strip().lower()
    package_id = order_row.get("package_id") or ""
    if not email or not package_id:
        return None

    key = (email, package_id)
    candidates = airalo_index.get(key, [])
    if not candidates:
        return None

    # Date de la commande côté orders
    order_dt_str = order_row.get("created_at")
    try:
        order_dt = datetime.fromisoformat(order_dt_str.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        order_dt = None

    if not order_dt:
        # Si on ne peut pas parser, on prend juste le dernier en date Airalo
        try:
            candidates_sorted = sorted(
                candidates,
                key=lambda r: r.get("created_at") or "",
            )
            return candidates_sorted[-1]
        except Exception:
            return candidates[0]

    # On choisit le candidate Airalo dont la date est la plus proche
    best = None
    best_delta = None
    for r in candidates:
        a_dt_str = r.get("created_at")
        try:
            a_dt = datetime.fromisoformat(a_dt_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            a_dt = None
        if not a_dt:
            continue
        delta = abs((order_dt - a_dt).total_seconds())
        if best_delta is None or delta < best_delta:
            best = r
            best_delta = delta

    return best or (candidates[0] if candidates else None)


# 🛒 Sync commandes depuis `orders` + enrichissement eSIM via `airalo_orders`
def sync_orders():
    print("🛒 Sync commandes (table orders)...")

    # On charge toutes les commandes
    rows = supabase.table("orders").select("*").execute().data
    print(f"🧾 {len(rows)} lignes dans orders")

    # On prépare l’index airalo_orders
    airalo_index = build_airalo_index()

    for row in rows:
        email = (row.get("email") or "").strip().lower()
        package_id = row.get("package_id")
        if not email or not package_id:
            continue

        # 🔑 Référence de commande côté Odoo
        # priorité : airalo_order_id > stripe_session_id > id
        order_ref = row.get("airalo_order_id") or row.get("stripe_session_id") or row.get("id")

        # Statut de paiement
        status = (row.get("status") or "").lower()  # 'completed' = payé
        transaction_type = row.get("transaction_type") or ""  # new_order / topup

        # Déjà en Odoo ?
        existing = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "search",
            [[["client_order_ref", "=", order_ref]]],
            {"limit": 1},
        )
        if existing:
            continue

        partner_id = find_or_create_partner(
            email,
            full_name=(row.get("prenom") or "") + " " + (row.get("nom") or ""),
        )
        if not partner_id:
            print(f"⚠️ Impossible de créer/trouver le client pour {email}")
            continue

        product = find_product(package_id)
        if not product:
            print(f"❌ Produit introuvable pour commande : {package_id}")
            continue

        # Prix payé (en EUR)
        price_paid = float(row.get("price") or 0.0)

        # Date de commande
        date_order = normalize_odoo_datetime(row.get("created_at"))

        # 🔗 On essaie de récupérer la ligne Airalo correspondante
        airalo_row = match_airalo_row(row, airalo_index)

        # Note eSIM
        note_lines = []
        if airalo_row:
            iccid = airalo_row.get("sim_iccid")
            qr_url = airalo_row.get("qr_code_url")
            apple_url = airalo_row.get("apple_installation_url")
            esim_status = airalo_row.get("status")
            activated_at = airalo_row.get("activated_at")
            expires_at = airalo_row.get("expires_at")
            data_balance = airalo_row.get("data_balance")

            note_lines.append("⚙️ Détails eSIM Airalo :")
            if iccid:
                note_lines.append(f"- ICCID : {iccid}")
            if qr_url:
                note_lines.append(f"- QR Code : {qr_url}")
            if apple_url:
                note_lines.append(f"- Lien installation Apple : {apple_url}")
            if esim_status:
                note_lines.append(f"- Statut eSIM : {esim_status}")
            if activated_at:
                note_lines.append(f"- Activée le : {activated_at}")
            if expires_at:
                note_lines.append(f"- Expire le : {expires_at}")
            if data_balance:
                note_lines.append(f"- Data restante : {data_balance}")

        note_text = "\n".join(note_lines) if note_lines else ""

        # 🧾 Création de la commande Odoo
        sale_id = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "create",
            [{
                "partner_id": partner_id,
                "client_order_ref": order_ref,
                "date_order": date_order,
                "note": note_text,
                "order_line": [
                    (0, 0, {
                        "product_id": product["id"],
                        "name": product["name"],
                        "product_uom_qty": 1,
                        "price_unit": price_paid,
                    })
                ],
            }],
        )
        print(f"🟢 Commande créée : {order_ref} ({transaction_type})")

        # 💳 Si status = completed → on confirme la commande
        if status == "completed":
            try:
                models.execute_kw(
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "sale.order",
                    "action_confirm",
                    [[sale_id]],
                )
                print(f"💳 Commande confirmée (payée) : {order_ref}")
            except Exception as e:
                print(f"⚠️ Impossible de confirmer la commande {order_ref} : {e}")

    print("✅ Commandes synchronisées.")


# 🚀 MAIN
if __name__ == "__main__":
    print("🚀 Début synchronisation Supabase → Odoo")
    remove_duplicate_products()
    sync_airalo_packages()
    sync_orders()
    print("✅ Synchronisation terminée")
